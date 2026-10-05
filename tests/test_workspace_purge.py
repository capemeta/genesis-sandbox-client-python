import unittest
from unittest.mock import patch

from genesis_sandbox_client import Client
from genesis_sandbox_client.errors import APIError, ProtocolError
from genesis_sandbox_client.workspace_purge import purge_receipt, session_ownership


def receipt():
    return {"session_id": "s", "workspace_id": "w", "tenant_id": "t", "user_id": "u", "principal_id": "p",
            "operation_id": "op", "request_digest": "a" * 64, "status": "unknown", "profile_revision": "rev"}


class PurgeTests(unittest.TestCase):
    def test_ownership_is_closed_and_readonly(self):
        owner = {key: value for key, value in receipt().items() if key not in {"operation_id", "request_digest",
            "status"}}
        client = Client("https://sandbox.example")
        with patch.object(client, "_request", return_value=owner) as request:
            self.assertEqual(client.get_session_ownership("s"), owner)
            self.assertEqual(request.call_args.args[:2], ("GET", "/v1/sessions/s/ownership"))
            self.assertEqual(request.call_count, 1)
        for key in owner:
            invalid = dict(owner)
            del invalid[key]
            with self.subTest(missing=key), self.assertRaises(ProtocolError):
                session_ownership(invalid, "s")
        with self.assertRaises(ProtocolError):
            session_ownership({**owner, "stdout": "not granted"}, "s")

    def test_storage_lookup_is_not_bound_to_compute_ttl(self):
        client = Client("https://sandbox.example")
        with patch.object(client, "_request", return_value=receipt()) as request:
            self.assertEqual(client.lookup_storage_purge("w", operation_id="op", request_digest="a" * 64), receipt())
            self.assertEqual(request.call_args.args[0], "GET")
            self.assertTrue(request.call_args.args[1].startswith("/v1/workspaces/w/purges/op?"))
        with patch.object(client, "_request", side_effect=APIError(404, "NOT_FOUND", "missing")) as request:
            self.assertIsNone(client.lookup_storage_purge("w", operation_id="op", request_digest="a" * 64))
            self.assertEqual(request.call_count, 1)
        with patch.object(client, "_request", return_value=receipt()):
            with self.assertRaises(ProtocolError):
                client.lookup_storage_purge("other", operation_id="op", request_digest="a" * 64)

    def test_readonly_missing_never_submits(self):
        client = Client("https://sandbox.example")
        with patch.object(client, "_request", side_effect=APIError(404, "NOT_FOUND", "missing")) as request:
            self.assertIsNone(client.lookup_workspace_purge("s", operation_id="op", request_digest="a" * 64))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], "GET")

    def test_one_original_submission_preserves_unknown(self):
        client = Client("https://sandbox.example")
        with patch.object(client, "_request", return_value=receipt()) as request:
            self.assertEqual(client.purge_session_workspace("s", operation_id="op",
                request_digest="a" * 64)["status"], "unknown")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[2], {"operation_id": "op", "request_digest": "a" * 64})
        with patch.object(client, "_request") as request:
            with self.assertRaises(ValueError):
                client.purge_session_workspace("s", operation_id="../op", request_digest="a" * 64)
            request.assert_not_called()

    def test_strict_complete_identity(self):
        for key in receipt():
            value = receipt()
            del value[key]
            with self.subTest(missing=key), self.assertRaises(ProtocolError):
                purge_receipt(value, "s", "op", "a" * 64)
        for changes in ({"session_id": "other"}, {"status": "done"}, {"purged": True}, {"user_id": None}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                purge_receipt({**receipt(), **changes}, "s", "op", "a" * 64)
