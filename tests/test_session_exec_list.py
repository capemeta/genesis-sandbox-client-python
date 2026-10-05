import asyncio
import io
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from genesis_sandbox_client import Client
from genesis_sandbox_client.async_session import AsyncSandboxSession
from genesis_sandbox_client.session import SandboxSession
from genesis_sandbox_client.types import SandboxOptions


class Response(io.BytesIO):
    status = 200


class SessionExecListTests(unittest.TestCase):
    def test_sync_and_async_heartbeat_suspend_after_unknown_renewal(self):
        from genesis_sandbox_client import TransportError
        client = mock.Mock(spec=Client)
        client.renew_session.side_effect = TransportError("response lost")
        sync = SandboxSession(client, {"session_id": "original"}, SandboxOptions(heartbeat=False))
        sync._renew_interval = 0
        sync._renew_loop()
        self.assertIsInstance(sync.heartbeat_error, TransportError)
        self.assertTrue(sync._renew_stop.is_set())
        self.assertEqual(client.renew_session.call_count, 1)
        client.renew_session.reset_mock()
        asynchronous = AsyncSandboxSession(client, {"session_id": "original"}, SandboxOptions(heartbeat=False))
        asynchronous._renew_interval = 0.001
        asyncio.run(asynchronous._renew_loop())
        self.assertIsInstance(asynchronous.heartbeat_error, TransportError)
        self.assertTrue(asynchronous._renew_stop.is_set())
        self.assertEqual(client.renew_session.call_count, 1)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_all_mutation_verbs_are_single_send_after_connection_loss(self, urlopen):
        from genesis_sandbox_client import TransportError
        client = Client("https://sandbox.example", max_attempts=5, retry_base_delay=0)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                urlopen.reset_mock()
                urlopen.side_effect = OSError("response lost")
                with self.assertRaises(TransportError):
                    client._request(method, "/v1/resource", {"operation_id": "original"}, max_retries=5)
                self.assertEqual(urlopen.call_count, 1)
                urlopen.reset_mock()
                with self.assertRaises(TransportError):
                    client._raw_request(method, "/v1/resource")
                self.assertEqual(urlopen.call_count, 1)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_paginated_read_preserves_server_fields_and_encodes_identity(self, urlopen):
        payload = {"items": [{"exec_id": "original", "operation_id": "op", "session_id": "s/1", "status": "interrupted", "stop_confirmed": False}], "total": 1, "next_cursor": "next"}
        urlopen.return_value = Response(json.dumps(payload).encode())
        result = Client("https://sandbox.example").list_session_execs("s/1", limit=3, cursor="a+/&b")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(urlsplit(request.full_url).path, "/v1/sessions/s%2F1/execs")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"limit": ["3"], "cursor": ["a+/&b"]})
        self.assertEqual(result, payload)
        self.assertEqual(urlopen.call_count, 1)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_invalid_pagination_does_not_submit_request(self, urlopen):
        client = Client("https://sandbox.example")
        for limit in (True, 0, 101, 1.5):
            with self.assertRaises(ValueError):
                client.list_session_execs("session", limit=limit)
        for cursor in ("", 1):
            with self.assertRaises(ValueError):
                client.list_session_execs("session", cursor=cursor)
        urlopen.assert_not_called()

    def test_sync_and_async_helpers_only_read_the_original_session(self):
        payload = {"items": [], "total": 0}
        client = mock.Mock(spec=Client)
        client.list_session_execs.return_value = payload
        sync = object.__new__(SandboxSession)
        sync._client, sync._session_id = client, "original"
        self.assertEqual(sync.list_execs(limit=7, cursor="next"), payload)
        asynchronous = object.__new__(AsyncSandboxSession)
        asynchronous._client, asynchronous._session_id = client, "original"
        self.assertEqual(asyncio.run(asynchronous.list_execs()), payload)
        self.assertEqual(client.list_session_execs.call_args_list,
                         [mock.call("original", limit=7, cursor="next"), mock.call("original", limit=50, cursor=None)])

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_invalid_or_foreign_history_and_stop_scalar_fail_closed(self, urlopen):
        from genesis_sandbox_client import ProtocolError
        original = {"exec_id": "original", "operation_id": "op", "session_id": "session", "status": "interrupted", "stop_confirmed": False}
        for page in ({"items": [], "total": True}, {"items": [dict(original, session_id="foreign")], "total": 1}, {"items": [dict(original, stop_confirmed="true")], "total": 1}, {"items": [original, original], "total": 2}):
            urlopen.return_value = Response(json.dumps(page).encode())
            with self.assertRaises(ProtocolError):
                Client("https://sandbox.example").list_session_execs("session")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_original_lookup_rejects_foreign_operation_without_creating(self, urlopen):
        from genesis_sandbox_client import ProtocolError
        urlopen.return_value = Response(json.dumps({"exec_id": "original", "operation_id": "foreign", "session_id": "session", "status": "interrupted", "stop_confirmed": False}).encode())
        with self.assertRaises(ProtocolError):
            Client("https://sandbox.example").get_exec_by_operation("session", "original-op")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.args[0].method, "GET")


if __name__ == "__main__":
    unittest.main()
