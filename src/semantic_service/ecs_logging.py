"""Complete JSON records from every ECS thread and child process."""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO, cast

_WRITE_LOCK = threading.Lock()
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9.]{0,127}\Z")
_FRAME = re.compile(r'^\s*File "([^"]+)", line ([0-9]{1,7}), in (\S+)\s*$')
_ERROR = re.compile(r"^([A-Za-z_][A-Za-z_0-9.]{0,127})(?::.*)?$")


def _invalid() -> dict[str, object]:
    return {"operation": "search_server", "status": "invalid log record"}


def _identifier(value: object) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else "unknown"


def _encode(record: dict[str, object]) -> str:
    try:
        return json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n"
    except (ValueError, TypeError, RecursionError):
        return json.dumps(_invalid(), separators=(",", ":")) + "\n"


def exception_record(error: BaseException) -> dict[str, object]:
    """Keep code locations and exception class, never values, paths or source."""
    frames: list[dict[str, object]] = []
    trace = error.__traceback__
    while trace is not None:
        frames.append(
            {
                "module": _identifier(trace.tb_frame.f_globals.get("__name__")),
                "function": _identifier(trace.tb_frame.f_code.co_name),
                "line": trace.tb_lineno,
            }
        )
        del frames[:-8]
        trace = trace.tb_next
    return {
        "operation": "search_server",
        "status": "exception",
        "exception_type": _identifier(type(error).__name__),
        "frames": frames,
    }


def emit_record(record: dict[str, object]) -> None:
    # One write includes the newline. Children forward records over private IPC;
    # this lock belongs only to the parent, never to a process we may terminate.
    encoded = _encode(record)
    if isinstance(sys.stdout, JSONLineWriter):
        sys.stdout.record(json.loads(encoded))
        return
    with _WRITE_LOCK:
        sys.stdout.write(encoded)
        sys.stdout.flush()


class JSONLineWriter(io.TextIOBase):
    """Adapt legacy print(json.dumps(...)) without changing shared runtime code."""

    def __init__(self, emit: Callable[[dict[str, object]], None]) -> None:
        super().__init__()
        self.emit = emit
        self._local = threading.local()
        self._emit_lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def record(self, record: dict[str, object]) -> None:
        """Typed in-process records bypass the untrusted text adapter."""
        with self._emit_lock:
            self.emit(record)

    def write(self, text: str) -> int:
        pending = getattr(self._local, "pending", "") + text
        lines = pending.split("\n")
        self._local.pending = lines.pop()
        if len(self._local.pending) > 65536:
            self._local.pending = ""
            lines.append("invalid")
        for line in lines:
            if not line:
                continue
            if line == "Traceback (most recent call last):":
                self._local.frames = []
                continue
            frames = getattr(self._local, "frames", None)
            if frames is not None:
                match = _FRAME.fullmatch(line)
                if match is not None:
                    # Only the final filename is considered, never its directory.
                    module = match[1].replace("\\", "/").rsplit("/", 1)[-1]
                    module = module[:-3] if module.endswith(".py") else "unknown"
                    frames.append(
                        {
                            "module": _identifier(module),
                            "function": _identifier(match[3]),
                            "line": int(match[2]),
                        }
                    )
                    del frames[:-8]
                    continue
                if line[:1].isspace():
                    # Source lines and caret annotations may contain secrets.
                    continue
                match = _ERROR.fullmatch(line)
                self._local.frames = None
                if match is not None:
                    with self._emit_lock:
                        self.emit(
                            {
                                "operation": "search_server",
                                "status": "untrusted exception text",
                                "exception_type_hint": match[1],
                                "frames": frames,
                            }
                        )
                    continue
            try:
                if len(line) > 65536:
                    raise ValueError("record limit")
                row: object = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("object required")
                record = cast(dict[str, object], row)
                # json.loads accepts NaN/Infinity and overflowing float literals.
                # Reject recursively before a malformed record reaches IPC.
                json.dumps(record, allow_nan=False)
                if (
                    record.get("operation") == "search_server"
                    and record.get("status") == "exception"
                ):
                    record = {
                        "operation": "search_server",
                        "status": "untrusted exception record",
                        "exception_type_hint": _identifier(record.get("exception_type")),
                    }
            except (ValueError, TypeError, RecursionError):
                record = _invalid()
            with self._emit_lock:
                self.emit(record)
        return len(text)

    def flush(self) -> None:
        # print emits body and newline separately; never flush a partial record.
        pass


def install_stdout() -> None:
    original: TextIO = sys.stdout
    lock = threading.Lock()

    def emit(record: dict[str, object]) -> None:
        with lock:
            original.write(_encode(record))
            original.flush()

    sys.stdout = JSONLineWriter(emit)
    sys.stderr = JSONLineWriter(emit)


def slot_metric(
    utilization: float, *, namespace: str, cluster: str, service: str
) -> dict[str, object]:
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["ClusterName", "ServiceName"]],
                    "Metrics": [{"Name": "SearchSlotUtilization", "Unit": "Percent"}],
                }
            ],
        },
        "operation": "search_capacity",
        "ClusterName": cluster,
        "ServiceName": service,
        "SearchSlotUtilization": min(100.0, max(0.0, utilization)),
    }
