"""Persistent failures must not remove supervision or consume Search slots."""

import threading
import time
from types import SimpleNamespace

import pytest
import test_ecs_worker_recovery as fixtures

from semantic_service import ecs_workers
from semantic_service.ecs_workers import ProcessApplication, WorkerPool


def test_persistent_supervise_once_failure_keeps_supervision_and_recovers(monkeypatch):
    with WorkerPool(fixtures.simple_factory, size=2, startup_seconds=5) as pool:
        original = pool._supervise_once
        observed = []

        def fail_persistently():
            observed.append(True)
            raise OSError("synthetic persistent supervisor fault")

        with monkeypatch.context() as patch:
            patch.setattr(pool, "_supervise_once", fail_persistently)
            fixtures.eventually(lambda: len(observed) >= 4, seconds=2)
            assert pool._supervisor.is_alive()
            assert pool._starting == 0
        # Removing the fault must restore real capacity without an incoming request.
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        fixtures.eventually(
            lambda: victim not in pool._all and len(pool._slots) == 2 and pool._starting == 0
        )
        assert pool._supervisor.is_alive() and pool.healthy()
        assert pool._supervise_once == original


@pytest.mark.parametrize("failure", [MemoryError, RecursionError, BrokenPipeError])
def test_supervisor_error_handler_contains_record_construction_failures(monkeypatch, failure):
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        observed = []
        records = []

        def fail_persistently():
            observed.append(True)
            raise OSError("synthetic persistent supervisor fault")

        def cannot_make_record(error):
            records.append(type(error).__name__)
            raise failure("synthetic telemetry construction failure")

        with monkeypatch.context() as patch:
            patch.setattr(pool, "_supervise_once", fail_persistently)
            patch.setattr(ecs_workers, "exception_record", cannot_make_record)
            fixtures.eventually(lambda: len(observed) >= 4, seconds=2)
            assert len(records) >= 4
            assert pool._supervisor.is_alive() and pool._starting == 0
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        fixtures.eventually(lambda: victim not in pool._all and pool.healthy())


@pytest.mark.parametrize("remaining", [0.002, 0.005, 0.010, 0.499])
def test_execute_subfloor_deadline_never_dispatches_a_healthy_search_worker(remaining):
    app = object.__new__(ProcessApplication)
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        app.search = pool
        before = {slot.process.pid for slot in pool._all}
        result = app.execute(SimpleNamespace(end=time.monotonic() + remaining))
        assert result == {
            "version": 1,
            "ok": False,
            "error": "deadline exceeded",
            "retryable": True,
        }
        assert {slot.process.pid for slot in pool._all} == before
        assert all(slot.process.is_alive() for slot in pool._all)
        assert pool._busy == 0 and pool.healthy()


def test_execute_dispatches_when_enough_budget_remains():
    calls = []
    app = object.__new__(ProcessApplication)
    app.search = SimpleNamespace(run=lambda *args: calls.append(args) or {"ok": True})
    admitted = SimpleNamespace(end=time.monotonic() + 2)
    assert app.execute(admitted) == {"ok": True}
    assert calls == [("execute", admitted, admitted.end)]


def test_shutdown_never_closes_a_connection_while_replacement_owns_its_poll(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    marker = tmp_path / "replacement-init"
    pool = WorkerPool(fixtures.ControlledFactory(str(marker)), size=1, startup_seconds=5)
    original_context = pool._context
    poll_entered = threading.Event()
    release_poll = threading.Event()
    close_attempted = threading.Event()
    violations = []
    original_close = ecs_workers._close_connection

    def close(slot):
        if isinstance(slot.connection, Connection) and slot.connection.polling:
            if not slot.connection_lock.locked():
                violations.append("poll does not own the connection lock")
            close_attempted.set()
        original_close(slot)

    monkeypatch.setattr(ecs_workers, "_close_connection", close)

    class Connection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.polling = False

        def poll(self, timeout):
            self.polling = True
            poll_entered.set()
            try:
                assert release_poll.wait(3)
                return self.wrapped.poll(timeout)
            finally:
                self.polling = False

        def recv(self):
            return self.wrapped.recv()

        def close(self):
            if self.polling:
                violations.append("closed during poll")
            self.wrapped.close()

    class Context:
        def Pipe(self, **kwargs):
            parent, child = original_context.Pipe(**kwargs)
            return Connection(parent), child

        def RawValue(self, *args):
            return original_context.RawValue(*args)

        def Process(self, **kwargs):
            return original_context.Process(**kwargs)

    try:
        marker.write_text("wait")
        pool._context = Context()
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        assert poll_entered.wait(3)
        with ThreadPoolExecutor(max_workers=1) as threads:
            closing = threads.submit(pool.close)
            assert close_attempted.wait(3)
            release_poll.set()
            closing.result(timeout=4)
        assert violations == []
        assert not pool._all and pool._starting == 0
    finally:
        release_poll.set()
        marker.unlink(missing_ok=True)
        pool.close()


def test_late_completion_marker_cannot_change_a_watchdog_unfinished_decision(monkeypatch):
    import test_ecs_workers as work

    with WorkerPool(work.factory, size=1, startup_seconds=5) as pool:
        slot = pool._slots[0]
        original = slot.connection
        reaped = threading.Event()
        original_reap = ecs_workers._watchdog_reap

        def reap_after_late_marker(victim):
            # The watchdog observed an unfinished Search. Model completion
            # racing its kill, before the parent receives the resulting EOF.
            victim.completion.value = 1
            original_reap(victim)
            reaped.set()

        class Connection:
            def send(self, value):
                original.send(value)

            def poll(self, timeout):
                assert reaped.wait(2)
                return original.poll(timeout)

            def recv(self):
                return original.recv()

            def close(self):
                original.close()

        slot.connection = Connection()
        monkeypatch.setattr(ecs_workers, "_watchdog_reap", reap_after_late_marker)
        response = pool.run("execute", "wait", time.monotonic() + 0.2)
        assert response == {
            "version": 1,
            "ok": False,
            "error": "deadline exceeded",
            "retryable": True,
        }


def test_supervisor_survives_an_unexpected_failure_in_its_error_handler(monkeypatch):
    with WorkerPool(fixtures.simple_factory, size=1, startup_seconds=5) as pool:
        observed = []

        def fail_persistently():
            observed.append(True)
            raise OSError("synthetic persistent supervisor fault")

        def broken_handler(record):
            raise MemoryError("synthetic failure in supervisor handler")

        with monkeypatch.context() as patch:
            patch.setattr(pool, "_supervise_once", fail_persistently)
            patch.setattr(ecs_workers, "_emit", broken_handler)
            fixtures.eventually(lambda: len(observed) >= 4, seconds=2)
            assert pool._supervisor.is_alive() and pool.healthy()
        assert pool.run("execute", "recovered", time.monotonic() + 1)["value"] == "recovered"


@pytest.mark.parametrize("failure", [MemoryError, RecursionError, BrokenPipeError])
def test_exception_record_is_constructed_inside_the_telemetry_guard(monkeypatch, failure):
    observed = []

    def broken(error):
        observed.append(error)
        raise failure("synthetic record construction failure")

    monkeypatch.setattr(ecs_workers, "exception_record", broken)
    error = OSError("fixture")
    ecs_workers._emit(error)
    assert observed == [error]
