"""Synchronous Invoke ownership, including aborts before SDK response handoff.

The pinned Botocore streaming response hook owns the body before ``invoke``
returns. A BaseException can also interrupt earlier inside SDK transport, so
cleanup examines only known transport object types in that call's SDK frames.
No frame values, credentials, headers, endpoints or exception text are logged.
There is one invocation at a time, no background operations and no retry.
An interrupted Invoke discards only this dedicated client's HTTP pools after
closing checked-out resources. Session/model/credential caches remain reusable.
"""

from __future__ import annotations

import http.client
import signal
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType

from botocore.awsrequest import AWSResponse
from botocore.client import BaseClient
from botocore.response import StreamingBody
from urllib3.response import HTTPResponse


class _Resources:
    def __init__(self) -> None:
        self.values: list[object] = []
        self.seen: set[int] = set()

    def capture(self, value: object) -> None:
        if isinstance(value, StreamingBody):
            # close() alone closes its socket but does not restore the borrowed
            # pool slot when an unread body is discarded.
            self.capture(value._raw_stream)
        if isinstance(value, AWSResponse):
            value = value.raw
        if (
            isinstance(
                value,
                (
                    StreamingBody,
                    HTTPResponse,
                    http.client.HTTPResponse,
                    http.client.HTTPConnection,
                    socket.socket,
                ),
            )
            and id(value) not in self.seen
        ):
            self.seen.add(id(value))
            self.values.append(value)

    def capture_exception(self, trace: TracebackType | None) -> None:
        while trace is not None:
            frame = trace.tb_frame
            module = frame.f_globals.get("__name__", "")
            if isinstance(module, str) and (
                module.startswith(("botocore.", "urllib3."))
                or module in {"http.client", "ssl", "socket"}
            ):
                # Direct typed transport objects only; never traverse arbitrary
                # request dictionaries, user values or another call's state.
                for value in tuple(frame.f_locals.values()):
                    self.capture(value)
            trace = trace.tb_next

    def close(self) -> None:
        for value in self.values:
            try:
                if isinstance(value, StreamingBody):
                    value.close()
                elif isinstance(value, HTTPResponse):
                    value.close()
                    value.release_conn()
                elif isinstance(
                    value, (http.client.HTTPResponse, http.client.HTTPConnection, socket.socket)
                ):
                    value.close()
            # Continue closing independent owners even if one close fails.
            except Exception:  # nosec B110
                # Continue disposing the other independent owners. SIGALRM is
                # blocked by the caller while this finite cleanup runs.
                pass
        self.values.clear()
        self.seen.clear()


class InvokeOwnership:
    def __init__(self, client: object) -> None:
        self._active: _Resources | None = None
        self._lock = threading.Lock()
        self._client = client if isinstance(client, BaseClient) else None
        if isinstance(client, BaseClient):
            client.meta.events.register_first("before-parse.lambda.Invoke", self._capture_body)

    def _capture_body(self, response_dict: object, **kwargs: object) -> None:
        active = self._active
        if active is not None and isinstance(response_dict, dict):
            active.capture(response_dict.get("body"))

    @contextmanager
    def hold(self) -> Iterator[None]:
        # The calling facade runs on Lambda's main thread. Mask only ownership
        # transfer/cleanup, never network I/O, signing, parsing or body reading.
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        acquired = False
        failed = False
        owned = _Resources()
        try:
            acquired = self._lock.acquire(blocking=False)
            if not acquired:
                raise RuntimeError("concurrent fallback unavailable")
            self._active = owned
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
                yield
            except BaseException as error:
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
                failed = True
                owned.capture_exception(error.__traceback__)
                raise
            finally:
                signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
                owned.close()
                if failed and self._client is not None:
                    # An alarm can interrupt urllib3's own lease-return finally
                    # after its connection local has become None. Recreate only
                    # this single-flight client's HTTP pools instead of guessing
                    # queue counts or mutating another call's lease.
                    self._client.close()
                self._active = None
        finally:
            if acquired:
                self._lock.release()
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
