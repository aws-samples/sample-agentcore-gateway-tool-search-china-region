"""Worker capacity must recover independently of incoming Search requests."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from semantic_service.ecs_workers import ProcessApplication, WorkerPool


class SimpleWorker:
    def admit(self, request, *, authorization=None):
        if isinstance(request, dict) and "gate" in request:
            while not Path(request["gate"]).exists():
                time.sleep(0.005)
        return request

    def execute(self, value):
        return {"pid": os.getpid(), "value": value}


def simple_factory():
    return SimpleWorker()


@dataclass
class ControlledFactory:
    marker: str

    def __call__(self):
        path = Path(self.marker)
        if path.exists() and path.read_text() == "fail":
            raise RuntimeError("private initialization fixture")
        while path.exists():
            time.sleep(0.01)
        return SimpleWorker()


def eventually(predicate, seconds=5):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        time.sleep(0.01)
    assert predicate()


def test_idle_worker_death_recovers_without_any_request():
    with WorkerPool(simple_factory, size=2, startup_seconds=5) as pool:
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        eventually(
            lambda: victim not in pool._all
            and len(pool._slots) == 2
            and all(slot.process.is_alive() for slot in pool._slots)
        )
        assert pool.healthy()


def test_expired_before_dispatch_keeps_all_healthy_worker_pids():
    with WorkerPool(simple_factory, size=2, startup_seconds=5) as pool:
        before = {slot.process.pid for slot in pool._all}
        result = pool.run("execute", "never-dispatched", time.monotonic() - 1)
        assert result["error"] == "deadline exceeded"
        time.sleep(0.2)
        assert {slot.process.pid for slot in pool._all} == before
        assert all(slot.process.is_alive() for slot in pool._all)
        assert pool.utilization() == 0 and pool.healthy()


def test_replacement_warming_preserves_other_worker_capacity(tmp_path):
    marker = tmp_path / "initialization"
    with WorkerPool(ControlledFactory(str(marker)), size=2, startup_seconds=5) as pool:
        marker.write_text("wait")
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        time.sleep(0.15)
        assert pool.healthy()
        result = pool.run("execute", "survivor", time.monotonic() + 1)
        assert result["value"] == "survivor"
        marker.unlink()
        pool.wait_ready(5)
        assert pool.healthy()


def test_failed_replacements_back_off_and_recover_after_fault_clears(tmp_path, capsys):
    marker = tmp_path / "initialization"
    with WorkerPool(ControlledFactory(str(marker)), size=1, startup_seconds=5) as pool:
        marker.write_text("fail")
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        time.sleep(0.8)
        assert not pool.healthy()
        marker.unlink()
        pool.wait_ready(5)
        assert pool.healthy() and pool._failure_count == 0
        assert len(pool._all) == 1
        assert pool.run("execute", "recovered", time.monotonic() + 1)["value"] == "recovered"
    failures = capsys.readouterr().out.count('"status":"worker initialization failed"')
    assert 1 <= failures <= 6


def test_authentication_default_capacity_matches_search(monkeypatch, tmp_path):
    monkeypatch.setattr("semantic_service.ecs_workers._admission_factory", simple_factory)
    monkeypatch.setattr("semantic_service.ecs_workers._search_factory", simple_factory)
    app = ProcessApplication(workers=4)
    gate = tmp_path / "auth-release"
    run = app.admission.run
    # This test measures simultaneous capacity, not the production watchdog.
    # A fixture-only window keeps slow CI scheduling from expiring the first
    # request before the test can observe the fourth occupied slot.
    monkeypatch.setattr(
        app.admission,
        "run",
        lambda method, payload, end: run(method, payload, time.monotonic() + 30),
    )
    try:
        assert app.admission.size == app.search.size == 4
        with ThreadPoolExecutor(max_workers=4) as threads:
            jobs = [
                threads.submit(
                    app.admit,
                    {"gate": str(gate), "deadline_ms": int(time.time() * 1000) + 3000},
                )
                for _ in range(4)
            ]
            eventually(lambda: app.admission.utilization() == 100, seconds=10)
            gate.touch()
            assert all(job.result(timeout=2).get("gate") == str(gate) for job in jobs)
    finally:
        gate.touch()
        app.close()


@pytest.mark.parametrize(
    ("remaining_ms", "expected_end"),
    [(500, 124.0), (1750, 125.25), (4000, 127.5), (30000, 127.5)],
)
def test_admission_watchdog_deadline_uses_request_and_local_ceiling(
    monkeypatch, remaining_ms, expected_end
):
    from semantic_service import ecs_workers

    wall_now, monotonic_now = 1700000000.25, 123.5
    monkeypatch.setattr(
        ecs_workers,
        "time",
        SimpleNamespace(time=lambda: wall_now, monotonic=lambda: monotonic_now),
    )
    request = {"deadline_ms": int(wall_now * 1000) + remaining_ms}
    calls = []
    admitted = object()

    def run(method, payload, end):
        calls.append((method, payload, end))
        return admitted

    app = object.__new__(ProcessApplication)
    app.admission = SimpleNamespace(run=run)
    assert app.admit(request, authorization="Bearer fixture") is admitted
    # Unlike the separate capacity test, this observes the exact production end
    # passed into the admission watchdog instead of replacing it with 30 seconds.
    assert calls == [("admit", (request, "Bearer fixture"), pytest.approx(expected_end))]


@pytest.mark.parametrize("remaining_ms", [-1000, 0, 499])
def test_admission_watchdog_deadline_never_dispatches_expired_or_subfloor_request(
    monkeypatch, remaining_ms
):
    from semantic_service import ecs_workers

    wall_now = 1700000000.25
    monkeypatch.setattr(
        ecs_workers, "time", SimpleNamespace(time=lambda: wall_now, monotonic=lambda: 123.5)
    )
    calls = []
    app = object.__new__(ProcessApplication)
    app.admission = SimpleNamespace(run=lambda *args: calls.append(args))
    assert app.admit({"deadline_ms": int(wall_now * 1000) + remaining_ms}) == {
        "version": 1,
        "ok": False,
        "error": "deadline exceeded",
        "retryable": True,
    }
    assert calls == []


def test_shutdown_stops_supervisor_and_reaps_all_processes():
    pool = WorkerPool(simple_factory, size=2, startup_seconds=5)
    slots = tuple(pool._all)
    pool.close()
    assert all(not slot.process.is_alive() for slot in slots)
    assert not pool._supervisor.is_alive()


def test_authentication_capacity_cannot_be_configured_below_search(monkeypatch):
    monkeypatch.setattr("semantic_service.ecs_workers._admission_factory", simple_factory)
    monkeypatch.setattr("semantic_service.ecs_workers._search_factory", simple_factory)
    app = None
    try:
        with pytest.raises(ValueError, match="authentication"):
            app = ProcessApplication(workers=4, auth_workers=2)
    finally:
        if app is not None:
            app.close()


@pytest.mark.parametrize("stage", ["pipe", "process-start", "thread-start"])
def test_failed_replacement_creation_is_retried_without_losing_supervision(monkeypatch, stage):
    with WorkerPool(simple_factory, size=1, startup_seconds=5) as pool:
        context = pool._context
        calls = []

        class Context:
            def RawValue(self, *args):
                return context.RawValue(*args)

            def Pipe(self, **kwargs):
                if stage == "pipe" and len(calls) < 2:
                    calls.append(True)
                    raise OSError("private descriptor failure")
                return context.Pipe(**kwargs)

            def Process(self, **kwargs):
                process = context.Process(**kwargs)
                if stage == "process-start" and len(calls) < 2:
                    calls.append(True)

                    def fail_start():
                        raise OSError("private spawn failure")

                    process.start = fail_start
                return process

        real_start = threading.Thread.start

        def start(thread):
            if (
                stage == "thread-start"
                and getattr(thread, "_target", None) == pool._replace
                and len(calls) < 2
            ):
                calls.append(True)
                raise RuntimeError("private thread capacity failure")
            return real_start(thread)

        monkeypatch.setattr(threading.Thread, "start", start)
        pool._context = Context()
        victim = pool._slots[0]
        victim.process.kill()
        victim.process.join(1)
        eventually(lambda: len(calls) >= 2 and pool.healthy())
        assert pool._supervisor.is_alive()
        assert len(pool._all) == 1
        assert pool._starting == 0


def test_request_timer_creation_failure_cannot_lose_or_kill_a_healthy_slot(monkeypatch):
    with WorkerPool(simple_factory, size=1, startup_seconds=5) as pool:
        original = {slot.process.pid for slot in pool._all}
        real_start = threading.Timer.start

        def fail_request_timer(timer):
            if getattr(timer.function, "__name__", "") == "expire":
                raise RuntimeError("private temporary thread capacity")
            return real_start(timer)

        with monkeypatch.context() as patch:
            patch.setattr(threading.Timer, "start", fail_request_timer)
            result = pool.run("execute", "not-dispatched", time.monotonic() + 1)
        assert result["error"] == "capacity" and result["retryable"] is True
        assert pool.utilization() == 0 and pool.healthy()
        assert {slot.process.pid for slot in pool._all} == original
        assert pool.run("execute", "recovered", time.monotonic() + 1)["value"] == "recovered"


def test_expiry_while_starting_timer_does_not_dispatch_or_reap(monkeypatch):
    with WorkerPool(simple_factory, size=1, startup_seconds=5) as pool:
        original = {slot.process.pid for slot in pool._all}
        real_start = threading.Timer.start

        def delayed_start(timer):
            real_start(timer)
            if getattr(timer.function, "__name__", "") == "expire":
                time.sleep(0.08)

        with monkeypatch.context() as patch:
            patch.setattr(threading.Timer, "start", delayed_start)
            result = pool.run("execute", "not-dispatched", time.monotonic() + 0.04)
        assert result["error"] == "deadline exceeded"
        assert pool.utilization() == 0 and pool.healthy()
        assert {slot.process.pid for slot in pool._all} == original
