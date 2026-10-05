"""控制探测专用绝对读取截止；不改变其他业务调用的传输语义。"""

from __future__ import annotations

import http.client
import io
import socket
import time
import urllib.request
from typing import Any


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("control probe deadline exceeded")
    return remaining


class _DeadlineReader(io.RawIOBase):
    def __init__(self, raw: Any, connection: socket.socket, deadline: float) -> None:
        super().__init__()
        self.raw, self.connection, self.deadline = raw, connection, deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self.connection.settimeout(_remaining(self.deadline))
        return self.raw.readinto(buffer)

    def close(self) -> None:
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, connection: socket.socket, deadline: float) -> None:
        self.connection, self.deadline = connection, deadline

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def makefile(self, mode: str, buffering: int | None = None) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("control probe only supports binary response reads")
        # 使用原socket的file引用：HTTPConnection关闭时响应体仍由原file持有。
        raw = self.connection.makefile("rb", buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self.connection, self.deadline))


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, deadline: float, **kwargs: Any) -> None:
        self.deadline = deadline
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self.timeout = _remaining(self.deadline)
        super().connect()
        assert self.sock is not None
        self.sock.settimeout(_remaining(self.deadline))
        self.sock = _DeadlineSocket(self.sock, self.deadline)  # type: ignore[assignment]


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, deadline: float, **kwargs: Any) -> None:
        self.deadline = deadline
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        self.timeout = _remaining(self.deadline)
        super().connect()
        assert self.sock is not None
        self.sock.settimeout(_remaining(self.deadline))
        self.sock = _DeadlineSocket(self.sock, self.deadline)  # type: ignore[assignment]


def deadline_urlopen(request: urllib.request.Request, timeout_seconds: float) -> Any:
    from .client import _NoRedirect

    deadline = time.monotonic() + timeout_seconds

    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(lambda *args, **kwargs: _DeadlineHTTPConnection(
                *args, deadline=deadline, **kwargs), req)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(lambda *args, **kwargs: _DeadlineHTTPSConnection(
                *args, deadline=deadline, **kwargs), req)

    return urllib.request.build_opener(_NoRedirect(), HTTP(), HTTPS()).open(
        request, timeout=_remaining(deadline))
