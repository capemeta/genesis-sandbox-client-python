"""实际HTTP单发、能力字段严格解析；不冒充Docker隔离测试。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from genesis_sandbox_client import APIError, Client, ProtocolError, SandboxError
from genesis_sandbox_client.control_probe import parse_control_capabilities


def payload(**changes):
    return {"available": True, "runtime_kind": "docker", "default_runtime": "runc", "available_runtimes": ["runc"],
            "queue": {}, "execd": {"command": True, "session": True, "files": True, "metrics": True},
            "supports_runsc": False, "supports_network_none": True, "supports_resource_limits": True, **changes}


def test_control_probe_rejects_malformed_flags_and_missing_fields():
    for value in (payload(available="false"), payload(supports_resource_limits=1), payload(runtime_kind={}),
                  payload(unknown=True), {"available": True}, payload(execd={}), payload(available_runtimes="runc")):
        with pytest.raises(ProtocolError):
            parse_control_capabilities(value)
    assert not parse_control_capabilities(payload(available=False, available_runtimes=None)).available


def test_control_probe_real_http_is_single_bounded_authenticated_get():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            if len(calls) > 1:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":{"code":"UNAVAILABLE","message":"private-secret"}}')
                return
            data = json.dumps(payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", token="fixture-token", max_attempts=5)
        assert client.get_runtime_control_capabilities(timeout_seconds=2).supports_resource_limits
        with pytest.raises(APIError):
            client.get_runtime_control_capabilities(timeout_seconds=2)
        assert calls == [("/v1/sandbox/capabilities", "Bearer fixture-token")] * 2
        for budget in (True, 0, 31, float("inf"), float("nan")):
            with pytest.raises(ValueError):
                client.get_runtime_control_capabilities(timeout_seconds=budget)
        assert len(calls) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("phase", ["headers", "body", "error-body"])
def test_control_probe_real_slow_drip_cannot_extend_absolute_deadline(phase):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            try:
                if phase == "headers":
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                else:
                    self.connection.sendall(
                        (b"HTTP/1.1 503 Unavailable\r\n" if phase == "error-body" else b"HTTP/1.1 200 OK\r\n")
                        + b"Content-Length: 10000\r\nConnection: close\r\n\r\n{")
                for _ in range(100):
                    self.connection.sendall(b" ")
                    time.sleep(.03)
            except OSError:
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", max_attempts=5)
        started = time.monotonic()
        with pytest.raises(SandboxError):
            client.get_runtime_control_capabilities(timeout_seconds=.2)
        assert time.monotonic() - started < 1.5
        assert calls == ["/v1/sandbox/capabilities"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
