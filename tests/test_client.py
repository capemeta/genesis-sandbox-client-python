import inspect
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from genesis_sandbox_client import APIError, Client


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self._payload if size < 0 else self._payload[:size]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _StreamResponse(_Response):
    def __init__(self, lines):
        self.status = 200
        self.lines = [line.encode("utf-8") for line in lines]
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def close(self):
        self.closed = True


def _http_error(status=503):
    body = io.BytesIO(
        json.dumps(
            {
                "error_code": "EXECD_UNAVAILABLE",
                "message": "execd unavailable",
                "request_id": "req_test",
                "details": {"retryable": True},
            }
        ).encode("utf-8")
    )
    return urllib.error.HTTPError("http://sandbox/v1/jobs", status, "error", {"Retry-After": "0"}, body)


class ClientContractTest(unittest.TestCase):
    def test_request_methods_do_not_expose_caller_identity(self):
        for method in (Client.lease, Client.submit_job, Client.create_workspace, Client.create_session):
            parameters = inspect.signature(method).parameters
            self.assertNotIn("tenant_id", parameters)
            self.assertNotIn("user_id", parameters)

    def test_rejects_relative_base_url(self):
        with self.assertRaises(ValueError):
            Client("localhost:18010")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_post_is_not_retried_and_error_is_structured(self, urlopen):
        urlopen.side_effect = _http_error()
        client = Client("http://127.0.0.1:18010", max_attempts=5, retry_base_delay=0)

        with self.assertRaises(APIError) as caught:
            client.submit_job(code="print(1)")

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(caught.exception.error_code, "EXECD_UNAVAILABLE")
        self.assertEqual(caught.exception.request_id, "req_test")
        self.assertTrue(caught.exception.retryable)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resume_unknown_does_not_replay_runtime_creation(self, urlopen):
        urlopen.side_effect = [
            _http_error(),
            _Response(200, {"session_id": "session-1", "status": "active", "active_sandbox_id": "sandbox-1"}),
        ]
        client = Client("http://127.0.0.1:18010", max_attempts=2, retry_base_delay=0)

        with self.assertRaises(APIError):
            client.resume_session("session-1")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(
            urllib.parse.urlparse(urlopen.call_args.args[0].full_url).path,
            "/v1/sessions/session-1:resume",
        )

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_get_retries_transient_response(self, urlopen):
        urlopen.side_effect = [
            _http_error(),
            _Response(200, {"job_id": "job-1", "status": "succeeded"}),
        ]
        client = Client("http://127.0.0.1:18010", max_attempts=3, retry_base_delay=0)

        result = client.get_job("job-1")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(urlopen.call_count, 2)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_exec_lookup_is_scoped_by_session_and_operation_id(self, urlopen):
        urlopen.return_value = _Response(
            200,
            {
                "exec_id": "exec-1",
                "session_id": "session-1",
                "operation_id": "operation-1",
                "status": "running",
            },
        )
        result = Client("http://127.0.0.1:18010").get_exec_by_operation("session-1", "operation-1")
        request = urlopen.call_args.args[0]
        parsed = urllib.parse.urlparse(request.full_url)
        self.assertEqual(parsed.path, "/v1/sessions/session-1/execs:lookup")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"operation_id": ["operation-1"]})
        self.assertEqual(result["exec_id"], "exec-1")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_post_with_idempotency_key_does_not_replay_unknown_submission(self, urlopen):
        urlopen.side_effect = [
            _http_error(),
            _Response(200, {"job_id": "job-1", "status": "queued"}),
        ]
        client = Client("http://127.0.0.1:18010", max_attempts=3, retry_base_delay=0)
        with self.assertRaises(APIError):
            client.submit_job(code="print(1)", idempotency_key="request-1")
        self.assertEqual(urlopen.call_count, 1)

    def test_wait_job_is_bounded(self):
        client = Client("http://127.0.0.1:18010")
        client.get_job = lambda _: {"status": "running"}
        with self.assertRaises(TimeoutError):
            client.wait_job("job-1", max_wait=0)

    def test_wait_job_returns_interrupted_as_terminal(self):
        client = Client("http://127.0.0.1:18010")
        client.get_job = lambda _: {"job_id": "job-1", "status": "interrupted"}

        result = client.wait_job("job-1", poll_interval=0.001, max_wait=1)

        self.assertEqual(result["status"], "interrupted")

    def test_selector_and_execution_are_validated_locally(self):
        client = Client("http://127.0.0.1:18010")
        with self.assertRaises(ValueError):
            client.submit_job(code="print(1)", profile="python", hints=["python"])
        with self.assertRaises(ValueError):
            client.submit_job()
        with self.assertRaises(ValueError):
            client.resolve_environment(profile="python", ttl_seconds=59)
        with self.assertRaises(ValueError):
            client.build_dependencies(language="python")
        with self.assertRaises(ValueError):
            client.build_dependencies(environment={"profile": {"name": "python"}})

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resolve_environment_uses_request_envelope(self, urlopen):
        urlopen.return_value = _Response(
            200,
            {
                "resolution_id": "res-1",
                "profile_name": "office-basic",
            },
        )
        client = Client("http://127.0.0.1:18010")
        client.resolve_environment(profile="office-basic", ttl_seconds=120)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["environment"]["profile"]["name"], "office-basic")
        self.assertEqual(payload["ttl_seconds"], 120)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resolve_environment_can_pin_catalog_revision(self, urlopen):
        urlopen.return_value = _Response(
            200,
            {
                "resolution_id": "res-1",
                "profile_name": "office-basic",
                "profile_revision": "rev-1",
            },
        )
        client = Client("http://127.0.0.1:18010")
        client.resolve_environment(profile="office-basic", profile_revision="rev-1")
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["environment"]["profile"]["revision"], "rev-1")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resolve_environment_can_bind_product_default(self, urlopen):
        urlopen.return_value = _Response(
            200,
            {
                "resolution_id": "res-1",
                "profile_name": "code-basic",
            },
        )
        Client("http://127.0.0.1:18010").resolve_environment()
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["environment"], {"hints": {}})

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_job_logs_are_parsed_incrementally_and_closed(self, urlopen):
        response = _StreamResponse(
            [
                "id: 7\n",
                "event: stdout\n",
                'data: {"message":"hello"}\n',
                "\n",
            ]
        )
        urlopen.return_value = response
        client = Client("http://127.0.0.1:18010")

        events = list(client.job_logs("job-1", cursor=6))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, "7")
        self.assertEqual(events[0].event, "stdout")
        self.assertEqual(events[0].json()["message"], "hello")
        self.assertTrue(response.closed)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "text/event-stream")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_create_session_rejects_persistent_environment(self, urlopen):
        with self.assertRaises(TypeError):
            Client("http://127.0.0.1:18010").create_session(env={"FOO": "bar"})
        urlopen.assert_not_called()

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_patch_session_context_rejects_persistent_environment(self, urlopen):
        with self.assertRaises(TypeError):
            Client("http://127.0.0.1:18010").patch_session_context("session-1", env={"FOO": "bar"})
        urlopen.assert_not_called()

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_exec_unknown_keeps_operation_id_without_replaying(self, urlopen):
        urlopen.side_effect = [OSError("connection reset"), _Response(200, {"exec_id": "exec-1"})]
        client = Client("http://127.0.0.1:18010", retry_base_delay=0)
        from genesis_sandbox_client import TransportError

        with self.assertRaises(TransportError):
            client.exec_session_async("sess-1", code="print(1)", operation_id="op-1")
        self.assertEqual(urlopen.call_count, 1)
        for call in urlopen.call_args_list:
            self.assertEqual(json.loads(call.args[0].data)["operation_id"], "op-1")


class SessionLookupTests(unittest.TestCase):
    """operation lookup：会话创建响应丢失后的恢复（GET /v1/sessions:lookup）。"""

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_lookup_session_hit(self, urlopen):
        urlopen.return_value = _Response(
            200, {"session_id": "session-1", "idempotency_key": "request-1", "status": "active"}
        )
        result = Client("http://127.0.0.1:18010").lookup_session("request-1")
        request = urlopen.call_args.args[0]
        parsed = urllib.parse.urlparse(request.full_url)
        self.assertEqual(parsed.path, "/v1/sessions:lookup")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"idempotency_key": ["request-1"]})
        self.assertEqual(result["session_id"], "session-1")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_lookup_session_not_found_returns_none(self, urlopen):
        urlopen.side_effect = _http_error(404)
        result = Client("http://127.0.0.1:18010").lookup_session("request-404")
        self.assertIsNone(result)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_lookup_session_other_errors_still_raise(self, urlopen):
        urlopen.side_effect = _http_error(503)
        client = Client("http://127.0.0.1:18010", max_attempts=1, retry_base_delay=0)
        with self.assertRaises(APIError) as caught:
            client.lookup_session("request-503")
        self.assertEqual(caught.exception.status_code, 503)

    def test_lookup_session_requires_key(self):
        with self.assertRaises(ValueError):
            Client("http://127.0.0.1:18010").lookup_session("")


class TokenProviderTests(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_token_provider_rotates_per_request(self, urlopen):
        urlopen.return_value = _Response(200, {"job_id": "job-1"})
        tokens = iter(["tok-1", "tok-2"])
        client = Client("http://127.0.0.1:18010", token_provider=lambda: next(tokens))

        client.get_job("job-1")
        client.get_job("job-1")

        auth_headers = [call.args[0].get_header("Authorization") for call in urlopen.call_args_list]
        self.assertEqual(auth_headers, ["Bearer tok-1", "Bearer tok-2"])

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_token_provider_takes_precedence_over_fixed_token(self, urlopen):
        urlopen.return_value = _Response(200, {"job_id": "job-1"})
        client = Client("http://127.0.0.1:18010", token="fixed", token_provider=lambda: "rotating")

        client.get_job("job-1")

        self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer rotating")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_empty_provider_value_falls_back_to_fixed_token(self, urlopen):
        urlopen.return_value = _Response(200, {"job_id": "job-1"})
        client = Client("http://127.0.0.1:18010", token="fixed", token_provider=lambda: "")

        client.get_job("job-1")

        self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer fixed")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_invalid_provider_token_is_rejected(self, urlopen):
        client = Client("http://127.0.0.1:18010", token_provider=lambda: "bad\r\ntoken")
        with self.assertRaises(ValueError):
            client.get_job("job-1")
        urlopen.assert_not_called()

    def test_invalid_fixed_token_is_rejected(self):
        with self.assertRaises(ValueError):
            Client("http://127.0.0.1:18010", token=" bad ")

    def test_non_callable_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            Client("http://127.0.0.1:18010", token_provider="tok")


class EndpointSecurityTests(unittest.TestCase):
    """与 Platform 端点约束一致：TLS / 回环 / 无内嵌凭据与路径。"""

    def test_https_remote_endpoint_allowed(self):
        Client("https://sandbox.example.com")

    def test_loopback_http_allowed(self):
        for url in ("http://localhost:18010", "http://127.0.0.1:18010", "http://[::1]:18010"):
            with self.subTest(url=url):
                Client(url)

    def test_non_loopback_http_rejected(self):
        for url in ("http://sandbox.example.com", "http://10.0.0.8:18010", "http://sandbox"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Client(url)

    def test_url_with_path_rejected(self):
        for url in ("https://sandbox.example.com/api", "http://127.0.0.1:18010/base/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Client(url)

    def test_url_with_credentials_query_or_fragment_rejected(self):
        for url in (
            "https://user:pass@sandbox.example.com",
            "https://sandbox.example.com?x=1",
            "https://sandbox.example.com#frag",
            "http://127.0.0.1:18010?x=1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Client(url)


class RedirectCredentialTests(unittest.TestCase):
    """Bearer 只发往配置的 base_url，禁止随重定向转发。"""

    def test_redirect_is_refused_and_credentials_never_forwarded(self):
        import http.server
        import threading

        hits = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get("Authorization")))
                if self.path == "/v1/jobs/redirect-me":
                    self.send_response(302)
                    self.send_header("Location", "/v1/leak")
                    self.end_headers()
                    return
                body = b'{"job_id": "job-1"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(f"http://127.0.0.1:{server.server_address[1]}", token="secret-token")
            # 走真实 urllib 路径（含 _NoRedirect），不 mock _urlopen。
            with self.assertRaises(APIError) as caught:
                client.get_job("redirect-me")
            self.assertIn(caught.exception.status_code, (301, 302, 303, 307, 308))
            self.assertEqual([path for path, _ in hits], ["/v1/jobs/redirect-me"])
            # 首个请求携带 Bearer（目标是配置的 base_url）；重定向目标从未被请求，
            # 凭据不可能被转发到其他地址。
            self.assertEqual(hits[0][1], "Bearer secret-token")
        finally:
            server.shutdown()
            server.server_close()

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_client_requests_carry_bearer_only_toward_base_url(self, urlopen):
        urlopen.return_value = _Response(200, {"job_id": "job-1"})
        client = Client("http://127.0.0.1:18010", token="secret-token")
        client.get_job("job-1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:18010/"))


class ProtocolErrorTests(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_invalid_json_response_raises_protocol_error(self, urlopen):
        from genesis_sandbox_client import ProtocolError, TransportError

        urlopen.return_value = _Response(200, {})
        urlopen.return_value._payload = b"<html>not json</html>"
        client = Client("http://127.0.0.1:18010", max_attempts=1, retry_base_delay=0)

        with self.assertRaises(ProtocolError) as caught:
            client.get_job("job-1")

        self.assertIsInstance(caught.exception, TransportError)  # 兼容 except TransportError


class RawRequestTransientTests(unittest.TestCase):
    """_raw_request：429/5xx 响应合成为 HTTPError 后仍走瞬态重试与结构化错误。"""

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_transient_raw_status_is_retried_then_structured(self, urlopen):
        urlopen.return_value = _Response(429, {})
        client = Client("http://127.0.0.1:18010", max_attempts=2, retry_base_delay=0)

        with self.assertRaises(APIError) as caught:
            client.download_session_file("sess-1", "a.txt")

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class ListSessionExecsContractTest(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_list_session_execs_builds_query_and_parses_list(self, urlopen):
        urlopen.return_value = _Response(
            200,
            {
                "items": [
                    {"exec_id": "exec-1", "operation_id": "op-1", "session_id": "sess-1", "status": "succeeded"},
                    {"exec_id": "exec-2", "operation_id": "op-2", "session_id": "sess-1", "status": "running"},
                ],
                "total": 2,
                "next_cursor": "cur-2",
            },
        )
        client = Client("http://127.0.0.1:18010")

        result = client.list_session_execs("sess-1", limit=50, cursor="cur-1")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:18010/v1/sessions/sess-1/execs?limit=50&cursor=cur-1",
        )
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["next_cursor"], "cur-2")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_list_session_execs_omits_empty_params(self, urlopen):
        urlopen.return_value = _Response(200, {"items": [], "total": 0, "next_cursor": ""})
        client = Client("http://127.0.0.1:18010")

        client.list_session_execs("sess with space")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:18010/v1/sessions/sess%20with%20space/execs",
        )
