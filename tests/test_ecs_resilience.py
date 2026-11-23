"""Recovery remains bounded when its own cleanup or telemetry fails."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import test_ecs_worker_recovery as fixtures

from semantic_service import ecs_workers
from semantic_service.ecs_workers import ProcessApplication, WorkerPool


@pytest.mark.parametrize("fault", ["_retire", "_reap", "emit_record"])
def test_supervisor_survives_cleanup_and_logging_faults(monkeypatch, fault):
    with WorkerPool(fixtures.simple_factory, size=2, startup_seconds=5) as pool:
        owner = pool if fault == "_retire" else ecs_workers
        original = getattr(owner, fault)
        injected = threading.Event()

        def fail_once(*args, **kwargs):
            if not injected.is_set():
                injected.set()
                raise BrokenPipeError("synthetic cleanup fault")
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, fault, fail_once)
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        fixtures.eventually(
            lambda: injected.is_set()
            and pool._supervisor.is_alive()
            and len(pool._slots) == 2
            and all(slot.process.is_alive() for slot in pool._slots)
        )
        assert pool.healthy()


@pytest.mark.parametrize("fault", ["_retire", "_reap", "emit_record"])
def test_replacement_failure_always_releases_starting_count(monkeypatch, tmp_path, fault):
    marker = tmp_path / "factory-failure"
    with WorkerPool(fixtures.ControlledFactory(str(marker)), size=1, startup_seconds=5) as pool:
        owner = pool if fault == "_retire" else ecs_workers
        original = getattr(owner, fault)
        victim = pool._slots[0]
        injected = threading.Event()

        def fail_once(*args, **kwargs):
            # Skip the supervisor's retirement of the deliberately killed slot;
            # fail inside a replacement's initialization/cleanup instead.
            replacing = threading.current_thread() is not pool._supervisor
            if replacing and not injected.is_set():
                injected.set()
                raise OSError("synthetic replacement cleanup fault")
            return original(*args, **kwargs)

        marker.write_text("fail")
        monkeypatch.setattr(owner, fault, fail_once)
        victim.process.kill()
        victim.process.join(1)
        fixtures.eventually(injected.is_set)
        marker.unlink()
        pool.wait_ready(5)
        fixtures.eventually(lambda: pool._starting == 0)
        assert pool.healthy()
        assert len(pool._all) == 1


def test_readiness_detects_a_dead_supervisor():
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        pool._stop.set()
        pool._supervisor.join(1)
        assert not pool.healthy()


def test_slot_connection_has_one_close_owner_under_shutdown_race():
    pool = WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5)
    slot = pool._slots[0]
    original = slot.connection
    entered = threading.Event()
    calls = []

    class Connection:
        def close(self):
            calls.append(True)
            entered.set()
            time.sleep(0.05)
            original.close()

    slot.connection = Connection()
    try:
        with ThreadPoolExecutor(max_workers=2) as threads:
            retiring = threads.submit(pool._retire, slot)
            assert entered.wait(1)
            closing = threads.submit(pool.close)
            retiring.result(timeout=3)
            closing.result(timeout=3)
        assert len(calls) == 1
    finally:
        pool.close()


def test_application_closes_search_when_admission_close_fails():
    called = []

    def broken():
        raise OSError("synthetic admission close failure")

    app = object.__new__(ProcessApplication)
    app.admission = SimpleNamespace(close=broken)
    app.search = SimpleNamespace(close=lambda: called.append("search"))
    with pytest.raises(OSError):
        app.close()
    assert called == ["search"]


def test_supervisor_does_not_lose_unstarted_batch_after_error_reporting_fails(monkeypatch):
    with WorkerPool(fixtures.simple_factory, size=2, startup_seconds=5) as pool:
        real_start = threading.Thread.start
        original_failed = pool._initialization_failed
        injected = threading.Event()
        reported = threading.Event()

        def start(thread):
            if getattr(thread, "_target", None) == pool._replace and not injected.is_set():
                injected.set()
                raise RuntimeError("synthetic start failure")
            return real_start(thread)

        def failed(error):
            if not reported.is_set():
                reported.set()
                raise OSError("synthetic failure handler fault")
            return original_failed(error)

        monkeypatch.setattr(threading.Thread, "start", start)
        monkeypatch.setattr(pool, "_initialization_failed", failed)
        with pool._condition:
            for slot in tuple(pool._slots):
                slot.process.kill()
                slot.process.join(1)
        fixtures.eventually(lambda: reported.is_set() and len(pool._slots) == 2)
        assert pool._starting == 0 and pool._supervisor.is_alive()


def test_tiny_future_deadline_does_not_dispatch_authentication_work():
    calls = []
    app = object.__new__(ProcessApplication)
    app.admission = SimpleNamespace(run=lambda *args: calls.append(args))
    result = app.admit({"deadline_ms": int(time.time() * 1000) + 5})
    assert result["error"] == "deadline exceeded" and result["retryable"] is True
    assert calls == []


def test_utilization_accounts_for_temporarily_unavailable_slots(tmp_path):
    marker, gate = tmp_path / "init", tmp_path / "release"
    with WorkerPool(fixtures.ControlledFactory(str(marker)), size=2, startup_seconds=5) as pool:
        marker.write_text("wait")
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        fixtures.eventually(lambda: victim not in pool._all and len(pool._slots) == 1)
        try:
            with ThreadPoolExecutor(max_workers=1) as threads:
                request = threads.submit(
                    pool.run, "admit", ({"gate": str(gate)}, None), time.monotonic() + 3
                )
                fixtures.eventually(lambda: pool._busy == 1)
                assert pool.utilization() == 100
                gate.touch()
                assert request.result(timeout=2)["gate"] == str(gate)
        finally:
            gate.touch()
            marker.unlink()


def failed_frame_worker(connection, factory, completion):
    connection.send(("ready", None))
    _method, payload = connection.recv()
    if payload == "completed":
        completion.value = 1
    connection.send(("failed", None))
    time.sleep(0.2)
    connection.close()


def test_explicit_failed_worker_frame_is_not_reported_as_deadline(monkeypatch):
    monkeypatch.setattr(ecs_workers, "_worker_main", failed_frame_worker)
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        result = pool.run("execute", "fixture", time.monotonic() + 2)
        assert result["error"] == "worker unavailable" and result["retryable"] is True


def test_second_thread_constructor_fault_cannot_strand_an_unstarted_reservation(monkeypatch):
    with WorkerPool(fixtures.simple_factory, size=2, startup_seconds=5) as pool:
        original = threading.Thread.__init__
        attempts = []

        def construct(thread, *args, **kwargs):
            if kwargs.get("target") == pool._replace:
                attempts.append(True)
                if len(attempts) == 2:
                    raise OSError("synthetic second thread construction failure")
            return original(thread, *args, **kwargs)

        monkeypatch.setattr(threading.Thread, "__init__", construct)
        with pool._condition:
            for slot in tuple(pool._slots):
                slot.process.kill()
                slot.process.join(1)
        fixtures.eventually(
            lambda: len(attempts) >= 2 and len(pool._slots) == 2 and pool._starting == 0
        )
        assert not any(thread.ident is None for thread in pool._rebuilders)
        assert pool._supervisor.is_alive()


@pytest.mark.parametrize("helper", ["Event", "Lock", "Timer"])
def test_request_helper_construction_failure_never_reserves_capacity(monkeypatch, helper):
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        original = getattr(threading, helper)
        failed = []
        before = {slot.process.pid for slot in pool._all}

        def construct(*args, **kwargs):
            if threading.current_thread() is threading.main_thread() and not failed:
                failed.append(True)
                raise RuntimeError("synthetic request helper allocation failure")
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(threading, helper, construct)
            try:
                pool.run("execute", "not dispatched", time.monotonic() + 1)
            except RuntimeError:
                pass
        assert failed
        assert pool._busy == 0 and len(pool._slots) == 1
        assert {slot.process.pid for slot in pool._all} == before
        assert pool.run("execute", "next", time.monotonic() + 1)["value"] == "next"


def test_failed_control_frame_after_completed_search_cannot_authorize_fallback(monkeypatch):
    monkeypatch.setattr(ecs_workers, "_worker_main", failed_frame_worker)
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        result = pool.run("execute", "completed", time.monotonic() + 2)
        assert result == {
            "version": 1,
            "ok": False,
            "error": "internal failure",
            "retryable": False,
        }
