"""Prewarmed, independently replaceable processes bound abandoned Search work."""

from __future__ import annotations

import multiprocessing
import random
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from types import TracebackType
from typing import Protocol, cast

from semantic_service.auth import ServiceError
from semantic_service.ecs_backend import (
    _SEARCH_COMPLETED,
    Admission,
    Backend,
    ClockSkew,
    DeadlineExceeded,
    ReplayUnavailable,
    backend_factory,
    failure,
)
from semantic_service.ecs_logging import JSONLineWriter, emit_record, exception_record
from semantic_service.route_budget import RESULT_DELIVERY_SECONDS

APPLICATION_STARTUP_SECONDS = 60.0


class _Completion(Protocol):
    value: int


def _emit(record: dict[str, object] | Exception) -> None:
    """Telemetry cannot interrupt resource ownership or recovery."""
    try:
        emit_record(exception_record(record) if isinstance(record, Exception) else record)
    # Telemetry is best effort; initialization failure is separately reported.
    except Exception:  # nosec B110
        pass


class Worker(Protocol):
    def admit(self, request: object, *, authorization: str | None = None) -> Admission: ...
    def execute(self, admitted: Admission) -> dict[str, object]: ...


def _search_factory() -> Backend:
    return backend_factory("ecs")


def _admission_factory() -> Backend:
    return backend_factory("admission")


def _worker_main(
    connection: Connection, factory: Callable[[], Worker], completion: _Completion
) -> None:
    # Each child has a separate pipe. Killing a writer cannot poison another
    # worker's queue or strand a lock used by the parent/other children.
    send_lock = threading.Lock()

    def send(kind: str, value: object) -> None:
        with send_lock:
            connection.send((kind, value))

    def forward(row: dict[str, object]) -> None:
        send("log", row)

    sys.stdout = JSONLineWriter(forward)
    sys.stderr = JSONLineWriter(forward)
    try:
        try:
            app = factory()
        except Exception as error:
            # Even EOFError/OSError here is initialization failure, not normal
            # parent-pipe shutdown. Report it before the transport-only handler.
            forward(exception_record(error))
            send("failed", None)
            return
        send("ready", None)
        while True:
            command: object = connection.recv()
            method, payload = cast(tuple[str, object], command)
            token = _SEARCH_COMPLETED.set(
                (lambda: setattr(completion, "value", 1)) if method == "execute" else None
            )
            try:
                if method == "admit":
                    request, authorization = cast(tuple[object, str | None], payload)
                    result: object = app.admit(request, authorization=authorization)
                elif method == "execute":
                    result = app.execute(cast(Admission, payload))
                else:
                    raise ValueError("unsupported private operation")
            except ServiceError as error:
                result = failure(
                    error.message,
                    retryable=isinstance(error, (DeadlineExceeded, ReplayUnavailable, ClockSkew)),
                )
            except Exception as error:
                forward(exception_record(error))
                result = failure("internal failure")
            finally:
                _SEARCH_COMPLETED.reset(token)
            send("result", result)
    except (EOFError, OSError):
        # Only pipe operations reach this boundary; initialization is handled above.
        pass
    except Exception as error:
        try:
            forward(exception_record(error))
            send("failed", None)
        except OSError:
            pass
    finally:
        connection.close()


@dataclass(eq=False)
class _Slot:
    process: BaseProcess
    connection: Connection
    reap_lock: threading.Lock
    connection_lock: threading.Lock = field(default_factory=threading.Lock)
    connection_closed: bool = False
    completion: _Completion | None = None


def _close_connection(slot: _Slot) -> None:
    # Set the state before calling close: after an OS error, retrying the same
    # descriptor number could close an unrelated, newly reused descriptor.
    with slot.connection_lock:
        if not slot.connection_closed:
            slot.connection_closed = True
            slot.connection.close()


def _receive(slot: _Slot, timeout: float) -> tuple[str, object] | None:
    # Reaping wakes a blocked read before close acquires this ownership lock.
    # A descriptor cannot be closed or reused while poll/recv still uses it.
    with slot.connection_lock:
        if slot.connection_closed:
            raise EOFError("worker connection is closed")
        if not slot.connection.poll(timeout):
            return None
        return cast(tuple[str, object], slot.connection.recv())


def _reap(slot: _Slot) -> bool:
    with slot.reap_lock:
        if slot.process.is_alive():
            slot.process.terminate()
        slot.process.join(0.1)
        if slot.process.is_alive():
            slot.process.kill()
            slot.process.join(0.2)
        return not slot.process.is_alive()


def _watchdog_reap(slot: _Slot) -> None:
    try:
        _reap(slot)
    except Exception as error:
        _emit(error)


class WorkerPool:
    """Bound work and independently restore missing capacity with capped backoff."""

    def __init__(
        self,
        factory: Callable[[], Worker],
        *,
        size: int,
        startup_seconds: float = APPLICATION_STARTUP_SECONDS,
        startup_end: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if not 1 <= size <= 8 or not 0 < startup_seconds <= 120:
            raise ValueError("invalid worker bounds")
        self.factory, self.size, self.startup_seconds = factory, size, startup_seconds
        self._startup_end = (
            startup_end if startup_end is not None else time.monotonic() + startup_seconds
        )
        self._initializing = True
        self._startup_cancel = stop_event if stop_event is not None else threading.Event()
        self._context = multiprocessing.get_context("spawn")
        self._condition = threading.Condition()
        self._slots: list[_Slot] = []
        self._all: set[_Slot] = set()
        self._busy = 0
        self._busy_slots: set[_Slot] = set()
        self._starting = 0
        self._rebuilders: set[threading.Thread] = set()
        self._retiring: set[_Slot] = set()
        self._unreclaimed: dict[_Slot, float] = {}
        self._closed = False
        self._failure_count = 0
        self._retry_at = 0.0
        self._stop = threading.Event()
        self._supervisor = threading.Thread(target=self._supervise, daemon=True)
        self._supervisor.start()
        try:
            self.wait_ready(max(0.0, self._startup_end - time.monotonic()))
            with self._condition:
                self._initializing = False
        except Exception:
            self.close()
            raise

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._supervise_once()
            except Exception as error:
                # Recovery must outlive even an unexpected handler failure,
                # including allocation before a telemetry helper is entered.
                try:
                    _emit(error)
                    _emit({"operation": "search_server", "status": "supervisor retry"})
                # Logging failure must not stop the supervisor recovery loop.
                except Exception:  # nosec B110
                    pass
            self._stop.wait(0.05)

    def _supervise_once(self) -> None:
        with self._condition:
            if self._closed or self._startup_cancel.is_set():
                return
            dead = [slot for slot in self._slots if not slot.process.is_alive()]
            for slot in dead:
                self._slots.remove(slot)
                self._retiring.add(slot)
            dead.extend(
                slot for slot, retry_at in self._unreclaimed.items() if time.monotonic() >= retry_at
            )
            for slot in dead:
                self._unreclaimed.pop(slot, None)
                self._retiring.add(slot)
        for slot in dead:
            self._safe_retire(slot)
        with self._condition:
            missing = (
                self.size
                - len(self._slots)
                - self._busy
                - self._starting
                - len(self._retiring)
                - len(self._unreclaimed)
            )
            if self._closed or time.monotonic() < self._retry_at:
                return
        # Construct, reserve and start one replacement at a time. A later
        # constructor failure cannot strand an earlier unstarted reservation.
        # Neither Thread construction nor Process creation holds the condition.
        for _ in range(max(0, missing)):
            thread: threading.Thread | None = None
            reserved = False
            try:
                thread = threading.Thread(target=self._replace, daemon=True)
                with self._condition:
                    if self._closed or self._startup_cancel.is_set():
                        return
                    self._rebuilders.add(thread)
                    self._starting += 1
                    reserved = True
                thread.start()
            except Exception as error:
                try:
                    self._initialization_failed(error)
                except Exception as secondary:
                    _emit(secondary)
                finally:
                    if reserved:
                        with self._condition:
                            self._starting -= 1
                            if thread is not None:
                                self._rebuilders.discard(thread)
                            self._condition.notify_all()

    def _initialization_failed(self, error: Exception) -> None:
        with self._condition:
            if not self._closed:
                self._failure_count += 1
                delay = min(5.0, 0.05 * 2 ** min(self._failure_count, 7))
                # Restart scheduling jitter is not a token or nonce.
                self._retry_at = time.monotonic() + delay * (0.8 + random.random() * 0.2)  # nosec B311
            self._condition.notify_all()
        _emit(error)
        _emit({"operation": "search_server", "status": "worker initialization failed"})

    def _safe_retire(self, slot: _Slot) -> None:
        try:
            self._retire(slot)
        except Exception as error:
            with self._condition:
                self._retiring.discard(slot)
                if slot in self._all:
                    self._unreclaimed[slot] = time.monotonic() + 1
                self._condition.notify_all()
            _emit(error)
            _emit({"operation": "search_server", "status": "worker reclaim failed"})

    def _retire(self, slot: _Slot) -> None:
        reaped = _reap(slot)
        try:
            if reaped:
                _close_connection(slot)
        finally:
            with self._condition:
                self._retiring.discard(slot)
                if reaped:
                    self._all.discard(slot)
                    self._unreclaimed.pop(slot, None)
                else:
                    self._unreclaimed[slot] = time.monotonic() + 1
                self._condition.notify_all()
        _emit(
            {
                "operation": "search_server",
                "status": "worker reaped" if reaped else "worker reclaim failed",
            }
        )

    def _replace(self) -> None:
        parent: Connection | None = None
        child: Connection | None = None
        slot: _Slot | None = None
        accepted = False
        try:
            parent, child = self._context.Pipe(duplex=True)
            completion = cast(_Completion, self._context.RawValue("b", 0))
            process = self._context.Process(
                target=_worker_main, args=(child, self.factory, completion), daemon=True
            )
            slot = _Slot(process, parent, threading.Lock(), completion=completion)
            with self._condition:
                if self._closed:
                    return
            process.start()
            with self._condition:
                self._all.add(slot)
                closed = self._closed
            child.close()
            if closed:
                return
            with self._condition:
                end = time.monotonic() + self.startup_seconds
                if self._initializing:
                    end = min(end, self._startup_end)
            timer = threading.Timer(max(0.0, end - time.monotonic()), _watchdog_reap, (slot,))
            timer.daemon = True
            timer.start()
            ready = False
            try:
                while True:
                    message = _receive(slot, max(0.0, end - time.monotonic()))
                    if message is None:
                        break
                    kind, value = message
                    if kind == "log":
                        _emit(cast(dict[str, object], value))
                    else:
                        ready = kind == "ready"
                        break
            finally:
                timer.cancel()
                timer.join(0.5)
            if not ready or not process.is_alive():
                raise RuntimeError("worker initialization failed")
            with self._condition:
                if not self._closed:
                    self._slots.append(slot)
                    accepted = True
                    if len(self._slots) + self._busy == self.size:
                        self._failure_count = 0
                        self._retry_at = 0.0
                    self._condition.notify_all()
                    return
        except Exception as error:
            try:
                self._initialization_failed(error)
            except Exception as secondary:
                _emit(secondary)
        finally:
            try:
                if child is not None:
                    try:
                        child.close()
                    except Exception as error:
                        _emit(error)
                if not accepted:
                    if slot is not None and slot.process.pid is not None:
                        self._safe_retire(slot)
                    elif parent is not None:
                        try:
                            _close_connection(slot) if slot is not None else parent.close()
                        except Exception as error:
                            _emit(error)
            finally:
                with self._condition:
                    self._starting -= 1
                    self._rebuilders.discard(threading.current_thread())
                    self._condition.notify_all()

    def wait_ready(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        with self._condition:
            while (
                sum(slot.process.is_alive() for slot in [*self._slots, *self._busy_slots])
                < self.size
            ):
                if self._closed or self._startup_cancel.is_set() or time.monotonic() >= end:
                    raise RuntimeError("worker capacity unavailable")
                self._condition.wait(min(0.05, max(0.0, end - time.monotonic())))

    def healthy(self) -> bool:
        with self._condition:
            return (
                not self._closed
                and self._supervisor.is_alive()
                and not self._unreclaimed
                and any(slot.process.is_alive() for slot in [*self._slots, *self._busy_slots])
            )

    def utilization(self) -> float:
        with self._condition:
            active = [slot for slot in [*self._slots, *self._busy_slots] if slot.process.is_alive()]
            busy = sum(slot in self._busy_slots for slot in active)
            return 100.0 * busy / len(active) if active else 0.0

    def run(self, method: str, payload: object, end: float) -> object:
        if time.monotonic() >= end:
            return failure("deadline exceeded", retryable=True)
        slot: _Slot
        healthy = True
        dispatched = False
        timer_started = False
        completion_at_expiry: bool | None = None
        try:
            completed = threading.Event()
            expired = threading.Event()
            completion_lock = threading.Lock()
        except (RuntimeError, OSError):
            _emit({"operation": "search_server", "status": "timer unavailable"})
            return failure("capacity", retryable=True)

        def search_completed() -> bool:
            with completion_lock:
                if completion_at_expiry is not None:
                    return completion_at_expiry
                return slot.completion is not None and slot.completion.value == 1

        def expire() -> None:
            nonlocal completion_at_expiry
            with completion_lock:
                if completed.is_set():
                    return
                expired.set()
                if not dispatched:
                    return
                # Decide once whether unfinished work needs reclamation. A late
                # child flag cannot change that decision after the kill begins.
                completion_at_expiry = slot.completion is not None and slot.completion.value == 1
            if completion_at_expiry and completed.wait(RESULT_DELIVERY_SECONDS):
                return
            _watchdog_reap(slot)

        try:
            timer = threading.Timer(max(0.0, end - time.monotonic()), expire)
            timer.daemon = True
        except (RuntimeError, OSError):
            _emit({"operation": "search_server", "status": "timer unavailable"})
            return failure("capacity", retryable=True)
        with self._condition:
            if time.monotonic() >= end:
                return failure("deadline exceeded", retryable=True)
            candidate = next((s for s in reversed(self._slots) if s.process.is_alive()), None)
            if self._closed or candidate is None:
                return failure("capacity", retryable=True)
            slot = candidate
            self._slots.remove(slot)
            self._busy += 1
            self._busy_slots.add(slot)
        try:
            if time.monotonic() >= end:
                return failure("deadline exceeded", retryable=True)
            try:
                timer.start()
                timer_started = True
            except RuntimeError:
                _emit({"operation": "search_server", "status": "timer unavailable"})
                return failure("capacity", retryable=True)
            with completion_lock:
                if expired.is_set() or time.monotonic() >= end:
                    return failure("deadline exceeded", retryable=True)
                dispatched = True
                healthy = False
                if slot.completion is not None:
                    slot.completion.value = 0
            with slot.connection_lock:
                if slot.connection_closed:
                    raise EOFError("worker connection is closed")
                slot.connection.send((method, payload))
            while True:
                # Admission.end uses the system CLOCK_MONOTONIC shared by the
                # deployed Linux processes (also exercised on macOS locally).
                # Completion itself is a shared flag, never a child timestamp.
                receive_end = end + (RESULT_DELIVERY_SECONDS if search_completed() else 0)
                message = _receive(slot, max(0.0, receive_end - time.monotonic()))
                if message is None:
                    if search_completed() and time.monotonic() < end + RESULT_DELIVERY_SECONDS:
                        continue
                    break
                kind, result = message
                if kind == "log":
                    _emit(cast(dict[str, object], result))
                elif kind == "result":
                    with completion_lock:
                        completed.set()
                        healthy = slot.process.is_alive()
                    return result
                else:
                    if search_completed():
                        _emit(
                            {"operation": "search_server", "status": "completed result unavailable"}
                        )
                        return failure("internal failure")
                    return failure("worker unavailable", retryable=True)
            if search_completed():
                _emit({"operation": "search_server", "status": "completed result unavailable"})
                return failure("internal failure")
            return failure("deadline exceeded", retryable=True)
        except (EOFError, OSError):
            if search_completed():
                _emit({"operation": "search_server", "status": "completed result unavailable"})
                return failure("internal failure")
            return failure(
                "deadline exceeded" if time.monotonic() >= end else "worker unavailable",
                retryable=True,
            )
        finally:
            timer.cancel()
            if timer_started:
                timer.join(0.5)
            healthy = healthy and slot.process.is_alive()
            with self._condition:
                if not healthy or self._closed:
                    self._retiring.add(slot)
                self._busy -= 1
                self._busy_slots.discard(slot)
                if healthy and not self._closed:
                    self._slots.append(slot)
                self._condition.notify_all()
            if not healthy or self._closed:
                self._safe_retire(slot)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            slots = tuple(self._all)
            rebuilders = tuple(self._rebuilders)
            self._slots.clear()
            self._condition.notify_all()
        self._stop.set()
        if self._supervisor is not threading.current_thread():
            self._supervisor.join(1)
        for slot in slots:
            self._safe_retire(slot)
        for thread in rebuilders:
            if thread is not threading.current_thread() and thread.ident is not None:
                thread.join(1)
        # A transient reap/close failure must not skip the remaining children.
        # Reattempt only slots whose process ownership is still unresolved.
        for _ in range(2):
            with self._condition:
                remaining = tuple(self._all)
            for slot in remaining:
                self._safe_retire(slot)

    def __enter__(self) -> WorkerPool:
        return self

    def __exit__(
        self,
        typ: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class ProcessApplication:
    def __init__(
        self,
        *,
        workers: int = 4,
        auth_workers: int | None = None,
        startup_seconds: float = APPLICATION_STARTUP_SECONDS,
        stop_event: threading.Event | None = None,
    ) -> None:
        if auth_workers is not None and auth_workers < workers:
            raise ValueError("authentication capacity cannot be below Search capacity")
        startup_end = time.monotonic() + startup_seconds
        self.admission = WorkerPool(
            _admission_factory,
            size=workers if auth_workers is None else auth_workers,
            startup_seconds=startup_seconds,
            startup_end=startup_end,
            stop_event=stop_event,
        )
        try:
            self.search = WorkerPool(
                _search_factory,
                size=workers,
                startup_seconds=startup_seconds,
                startup_end=startup_end,
                stop_event=stop_event,
            )
        except Exception:
            self.admission.close()
            raise

    def admit(self, request: object, *, authorization: str | None = None) -> object:
        from semantic_service.route_budget import ECS_SECONDS, MIN_ATTEMPT_SECONDS

        # The watchdog also covers blocked JWKS/secret reads before Search gets
        # a permit. A supplied wall deadline can only shorten the local ceiling.
        seconds = ECS_SECONDS
        if isinstance(request, dict) and type(request.get("deadline_ms")) is int:
            seconds = min(seconds, max(0.0, request["deadline_ms"] / 1000 - time.time()))
        if seconds < MIN_ATTEMPT_SECONDS:
            return failure("deadline exceeded", retryable=True)
        return self.admission.run("admit", (request, authorization), time.monotonic() + seconds)

    def execute(self, admitted: Admission) -> dict[str, object]:
        from semantic_service.route_budget import MIN_ATTEMPT_SECONDS

        # Authentication and replay protection can consume the useful Search
        # window. Reject before taking a slot, rather than kill a healthy worker
        # for work that cannot receive its minimum execution budget.
        if admitted.end - time.monotonic() < MIN_ATTEMPT_SECONDS:
            return failure("deadline exceeded", retryable=True)
        return cast(dict[str, object], self.search.run("execute", admitted, admitted.end))

    def healthy(self) -> bool:
        return self.admission.healthy() and self.search.healthy()

    def utilization(self) -> float:
        return self.search.utilization()

    def close(self) -> None:
        try:
            self.admission.close()
        finally:
            self.search.close()
