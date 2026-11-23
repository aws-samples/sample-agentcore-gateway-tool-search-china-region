"""Malformed metrics and exceptions cannot corrupt or disclose ECS records."""

import json
import os
import time
import traceback

import pytest

from semantic_service.ecs_logging import JSONLineWriter, emit_record


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_json_is_replaced_without_raising(number):
    rows = []
    writer = JSONLineWriter(rows.append)
    writer.write('{"operation":"fixture","nested":{"metric":' + number + "}}\n")
    assert rows == [{"operation": "search_server", "status": "invalid log record"}]


def test_direct_nonfinite_record_is_sanitized(capsys):
    emit_record({"operation": "fixture", "secret": "private fixture", "metric": float("nan")})
    assert json.loads(capsys.readouterr().out) == {
        "operation": "search_server",
        "status": "invalid log record",
    }


def test_traceback_retains_bounded_safe_code_metadata_without_error_text():
    rows = []
    writer = JSONLineWriter(rows.append)
    writer.write("Traceback (most recent call last):\n")
    for line in range(20):
        writer.write(
            f'  File "/private/username/customer/ecs_backend.py", line {line + 1}, in execute\n'
        )
        writer.write('    raise ValueError("private bearer query fixture")\n')
    writer.write("ValueError: private bearer query fixture\n")
    assert len(rows) == 1
    assert rows[0]["exception_type_hint"] == "ValueError"
    assert len(rows[0]["frames"]) == 8
    assert rows[0]["frames"][-1] == {
        "module": "ecs_backend",
        "function": "execute",
        "line": 20,
    }
    assert "private" not in json.dumps(rows)
    assert "username" not in json.dumps(rows)


def test_structured_exception_retains_class_and_safe_frames():
    from semantic_service.ecs_logging import exception_record

    try:
        raise ValueError("private bearer query fixture")
    except ValueError as error:
        row = exception_record(error)
    assert row["exception_type"] == "ValueError"
    assert (
        row["frames"][-1]["function"] == "test_structured_exception_retains_class_and_safe_frames"
    )
    assert row["frames"][-1]["module"] == __name__
    assert "private" not in json.dumps(row)
    assert "/" not in json.dumps(row)


class LoggingWorker:
    def execute(self, value):
        if value == "invalid":
            print('{"operation":"search","metric":NaN}', flush=True)
        elif value == "traceback":
            try:
                raise RuntimeError("private bearer query fixture")
            except RuntimeError:
                traceback.print_exc()
        elif value == "raised":
            raise LookupError("private bearer query fixture")
        return {"pid": os.getpid()}


def logging_factory():
    return LoggingWorker()


def test_bad_logs_do_not_kill_worker_and_exception_metadata_survives(capsys):
    from semantic_service.ecs_workers import WorkerPool

    with WorkerPool(logging_factory, size=1, startup_seconds=5) as pool:
        original = pool.run("execute", "ok", time.monotonic() + 2)
        for mode in ("invalid", "traceback"):
            assert pool.run("execute", mode, time.monotonic() + 2) == original
        assert pool.run("execute", "raised", time.monotonic() + 2)["error"] == "internal failure"
        assert pool.run("execute", "ok", time.monotonic() + 2) == original
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert "LookupError" in {r.get("exception_type") for r in rows}
    assert "RuntimeError" in {r.get("exception_type_hint") for r in rows}
    assert "private" not in captured.out + captured.err


def test_forged_traceback_cannot_be_mistaken_for_a_captured_exception():
    rows = []
    writer = JSONLineWriter(rows.append)
    writer.write(
        'Traceback (most recent call last):\n  File "fixture.py", line 1, in verify\n'
        "AuthenticationError: attacker text\n"
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "untrusted exception text"
    assert rows[0]["exception_type_hint"] == "AuthenticationError"
    assert "exception_type" not in rows[0]
