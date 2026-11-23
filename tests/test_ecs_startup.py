"""Startup uses one deadline and owns child cleanup before readiness."""

import multiprocessing
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import test_ecs_worker_recovery as fixtures

from semantic_service import ecs_workers
from semantic_service.ecs_workers import ProcessApplication, WorkerPool


@dataclass
class SlowFactory:
    seconds: float

    def __call__(self):
        time.sleep(self.seconds)
        return fixtures.SimpleWorker()


def test_two_pools_share_one_application_startup_deadline(monkeypatch):
    monkeypatch.setattr(ecs_workers, "_admission_factory", SlowFactory(0.5))
    monkeypatch.setattr(ecs_workers, "_search_factory", SlowFactory(0.5))
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="capacity"):
        ProcessApplication(workers=1, startup_seconds=0.8)
    assert time.monotonic() - started < 1.6
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_pool_startup_can_be_cancelled_without_waiting_for_full_deadline():
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    before = {child.pid for child in multiprocessing.active_children()}
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="capacity"):
            WorkerPool(SlowFactory(10), size=1, startup_seconds=5, stop_event=stop)
    finally:
        timer.cancel()
        timer.join(1)
    assert time.monotonic() - started < 1.5
    assert {child.pid for child in multiprocessing.active_children()} == before


@dataclass
class RecordingFactory:
    marker: str

    def __call__(self):
        Path(self.marker).write_text(str(os.getpid()))
        while True:
            time.sleep(0.05)


def starting_main(marker):
    from semantic_service import ecs_server

    sys.argv = ["ecs-server"]
    os.environ["SEMSEARCH_ECS_WORKERS"] = "1"
    ecs_workers._admission_factory = RecordingFactory(marker)
    raise SystemExit(ecs_server.main())


def test_sigterm_during_startup_reaps_spawned_worker(tmp_path):
    marker = tmp_path / "child-pid"
    context = multiprocessing.get_context("spawn")
    parent = context.Process(target=starting_main, args=(str(marker),))
    parent.start()
    pid = None
    try:
        fixtures.eventually(marker.exists)
        pid = int(marker.read_text())
        parent.terminate()
        parent.join(3)
        assert parent.exitcode == 0
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join(1)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
