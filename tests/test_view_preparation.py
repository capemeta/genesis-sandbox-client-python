import unittest
from unittest.mock import patch

from genesis_sandbox_client import Client
from genesis_sandbox_client.errors import APIError, ProtocolError
from genesis_sandbox_client.json_codec import loads
from genesis_sandbox_client.view_preparation import preparation_receipt


def receipt():
    return {"session_id": "s", "workspace_id": "w", "tenant_id": "t", "user_id": "u", "principal_id": "p",
            "operation_id": "op", "request_digest": "a" * 64, "status": "unknown", "profile_revision": "rev",
                "sandbox_id": ""}


class PreparationTests(unittest.TestCase):
    def test_lookup_absent_is_readonly_and_does_not_submit(self):
        client = Client("https://sandbox.example")
        with patch.object(client, "_request", side_effect=APIError(404, "NOT_FOUND", "missing")) as request:
            self.assertIsNone(client.lookup_workspace_preparation("s", operation_id="op", request_digest="a" * 64))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], "GET")
        with patch.object(client, "_request", side_effect=APIError(403, "AUTH_DENIED", "denied")):
            with self.assertRaises(APIError):
                client.lookup_workspace_preparation("s", operation_id="op", request_digest="a" * 64)

    def test_strict_receipt_rejects_substitution_and_unproved_facts(self):
        self.assertEqual(preparation_receipt(receipt(), "s", "op", "a" * 64)["status"], "unknown")
        for field in tuple(receipt()):
            missing = receipt()
            del missing[field]
            with self.subTest(missing=field), self.assertRaises(ProtocolError):
                preparation_receipt(missing, "s", "op", "a" * 64)
        for changes in ({"operation_id": "other"}, {"session_id": "other"}, {"status": "prepared"},
                        {"host_path": "/secret"}, {"facts": {}}, {"facts": None}, {"sandbox_id": None},
                            {"request_digest": "b" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                preparation_receipt({**receipt(), **changes}, "s", "op", "a" * 64)

    def test_invalid_request_never_sends(self):
        client = Client("https://sandbox.example")
        with patch.object(client, "_request") as request:
            for operation, digest in (("../op", "a" * 64), ("op", "bad"), (True, "a" * 64)):
                with self.assertRaises(ValueError):
                    client.prepare_workspace_view("s", operation_id=operation, request_digest=digest)
            request.assert_not_called()

    def test_response_json_rejects_nested_duplicates_and_nonfinite(self):
        for value in ('{"operation_id":"old","operation_id":"new"}', '{"facts":{"cpu":1,"cpu":2}}',
                      '{"resource_limits":{"cpu":NaN}}', '{"cpu":Infinity}', '{"cpu":1e309}'):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                loads(value)

    def test_prepared_proof_requires_fixed_facts(self):
        facts = {"session_id": "s", "workspace_id": "w", "profile_revision": "rev", "view_state": "prepared",
                 "os": "linux", "arch": "amd64", "image_digest": "sha256:" + "b" * 64, "network_mode": "deny",
                 "runtime_versions": {"python": "3.12"}, "runtime_executables": {"python": "/usr/bin/python"},
                 "resource_limits": {"cpu": 1}, "mechanisms": [], "readonly_regions": []}
        confirmed = {**receipt(), "status": "prepared", "sandbox_id": "container", "facts": facts}
        self.assertEqual(preparation_receipt(confirmed, "s", "op", "a" * 64)["status"], "prepared")
        for change in ({"host_path": "/secret"}, {"view_state": "sealed"}, {"resource_limits": {"cpu": True}},
                       {"resource_limits": {"cpu": 0}}, {"mechanisms": [False]},
                           {"runtime_versions": {"python": 3.12}}):
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                preparation_receipt({**confirmed, "facts": {**facts, **change}}, "s", "op", "a" * 64)
