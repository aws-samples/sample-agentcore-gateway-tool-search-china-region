"""Real child processes enforce cancellation and keep structured logs atomic."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def factory():
    return Work()


class Work:
    def execute(self, value):
        if value == "wait":
            while True:
                time.sleep(0.01)
        if value == "cpu":
            while True:
                sum(range(1000))
        if value == "concurrent-log-result":

            def log():
                for number in range(10):
                    print(
                        json.dumps(
                            {
                                "operation": "search",
                                "status": "ok",
                                "padding": "x" * 48000,
                                "number": number,
                            }
                        ),
                        flush=True,
                    )

            threading.Thread(target=log, daemon=True).start()
            return {"payload": "y" * 24000}
        if value == "stderr":
            import sys

            print("private bearer fixture", file=sys.stderr, flush=True)
        if value == "logs":
            for number in range(100):
                print(
                    json.dumps(
                        {
                            "operation": "search",
                            "status": "ok",
                            "attempt_id": "a" * 32,
                            "number": number,
                        }
                    ),
                    flush=True,
                )
        return {"pid": os.getpid(), "value": value}


@pytest.mark.parametrize("kind", ["wait", "cpu"])
def test_timeout_reaps_process_and_replaces_slot(kind):
    from semantic_service.ecs_workers import WorkerPool

    with WorkerPool(factory, size=1, startup_seconds=5) as pool:
        first = pool.run("execute", "ok", time.monotonic() + 2)
        assert first["pid"] != os.getpid()
        with ThreadPoolExecutor(max_workers=1) as threads:
            running = threads.submit(pool.run, "execute", kind, time.monotonic() + 0.2)
            for _ in range(100):
                if pool.utilization() == 100:
                    break
                time.sleep(0.005)
            assert pool.utilization() == 100
            result = running.result(timeout=2)
        assert result["error"] == "deadline exceeded" and result["retryable"] is True
        with pytest.raises(ProcessLookupError):
            os.kill(first["pid"], 0)
        pool.wait_ready(5)
        second = pool.run("execute", "ok", time.monotonic() + 2)
        assert second["pid"] != first["pid"]
        assert pool.utilization() == 0 and pool.healthy()


def test_records_are_atomic_for_threads_and_forwarded_child_runtime(capsys):
    from semantic_service.ecs_logging import JSONLineWriter
    from semantic_service.ecs_workers import WorkerPool

    rows = []
    stream = JSONLineWriter(rows.append)

    def emit(index):
        for number in range(100):
            print(
                json.dumps(
                    {
                        "operation": "search_backend",
                        "status": "unauthenticated",
                        "attempt_id": f"{index:032x}",
                        "number": number,
                    }
                ),
                file=stream,
                flush=True,
            )

    with ThreadPoolExecutor(max_workers=8) as threads:
        list(threads.map(emit, range(8)))
    assert len(rows) == 800
    assert all(row["status"] == "unauthenticated" for row in rows)
    assert len({(row["attempt_id"], row["number"]) for row in rows}) == 800
    with WorkerPool(factory, size=2, startup_seconds=5) as pool:
        with ThreadPoolExecutor(max_workers=2) as threads:
            list(threads.map(lambda _: pool.run("execute", "logs", time.monotonic() + 3), range(2)))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(row.get("operation") == "search" for row in records) == 200


def test_emf_is_task_normalized_and_includes_idle_zero():
    from semantic_service.ecs_logging import slot_metric

    for active, expected in [(0, 0), (1, 25), (4, 100)]:
        row = slot_metric(
            active / 4 * 100, namespace="semsearch/fixture", cluster="cluster", service="service"
        )
        assert row["SearchSlotUtilization"] == expected
        assert row["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["ClusterName", "ServiceName"]]
        assert row["ClusterName"] == "cluster" and row["ServiceName"] == "service"


def test_retired_processes_do_not_accumulate_and_idle_death_fails_readiness():
    from semantic_service.ecs_workers import WorkerPool

    with WorkerPool(factory, size=1, startup_seconds=5) as pool:
        pool.run("execute", "wait", time.monotonic() + 0.05)
        pool.wait_ready(5)
        assert len(pool._all) == 1
        slot = pool._slots[0]
        slot.process.kill()
        slot.process.join(1)
        assert not pool.healthy()


def test_admission_watchdog_uses_shorter_remote_deadline():
    from semantic_service.ecs_workers import ProcessApplication

    class Pool:
        def run(self, method, payload, end):
            assert 0.5 < end - time.monotonic() < 0.7

    app = object.__new__(ProcessApplication)
    app.admission = Pool()
    app.admit({"deadline_ms": int(time.time() * 1000) + 600}, authorization="fixture")


def test_worker_stderr_is_structured_and_sanitized(capsys):
    from semantic_service.ecs_workers import WorkerPool

    with WorkerPool(factory, size=1, startup_seconds=5) as pool:
        pool.run("execute", "stderr", time.monotonic() + 2)
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert any(row.get("status") == "invalid log record" for row in rows)
    assert "private bearer fixture" not in captured.out + captured.err


def test_background_large_logs_cannot_corrupt_result_frames(capsys):
    from semantic_service.ecs_workers import WorkerPool

    with WorkerPool(factory, size=1, startup_seconds=5) as pool:
        for _ in range(5):
            result = pool.run("execute", "concurrent-log-result", time.monotonic() + 3)
            assert result == {"payload": "y" * 24000}
    assert all(isinstance(json.loads(line), dict) for line in capsys.readouterr().out.splitlines())


def test_log_overflow_is_explicit_and_does_not_emit_empty_object():
    from semantic_service.ecs_logging import JSONLineWriter

    rows = []
    writer = JSONLineWriter(rows.append)
    writer.write("x" * 65537)
    writer.write("\n")
    assert rows == [{"operation": "search_server", "status": "invalid log record"}]


@pytest.mark.parametrize("error_type", [OSError, EOFError, RuntimeError])
def test_initialization_failure_is_reported_before_worker_exits(monkeypatch, error_type):
    """Factory I/O errors are startup failures, not an ordinary closed parent pipe."""
    import sys
    from types import SimpleNamespace

    from semantic_service.ecs_workers import _worker_main

    messages = []
    closed = []
    connection = SimpleNamespace(send=messages.append, close=lambda: closed.append(True))

    def fail():
        raise error_type("synthetic initialization failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "stdout", sys.stdout)
        scoped.setattr(sys, "stderr", sys.stderr)
        _worker_main(connection, fail, SimpleNamespace(value=0))
    assert ("failed", None) in messages
    assert not any(kind == "ready" for kind, _ in messages)
    assert any(kind == "log" for kind, _ in messages)
    assert closed == [True]
