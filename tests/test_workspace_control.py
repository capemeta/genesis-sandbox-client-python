import io
import json
import unittest
from unittest import mock

from genesis_sandbox_client import Client


class Response(io.BytesIO):
    status = 200


class WorkspaceControlContractTests(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_shared_workspace_explicit_detach_retention_does_not_send_owner_or_path(self, urlopen):
        urlopen.return_value = Response(b"{}")
        client = Client("https://sandbox.example")
        binding = {"mode": "shared", "storage_ref": "approved", "resource_id": "resource", "binding_version": 1}
        client.create_workspace("resource", quota_mb=64, workspace_binding=binding, retention_mode="explicit_delete")
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload, {"workspace_id": "resource", "quota_mb": 64,
                                  "workspace_binding": binding, "retention_mode": "explicit_delete"})
        with self.assertRaises(ValueError):
            client.create_workspace(retention_mode="auto")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_view_and_retained_history_use_authenticated_formal_routes(self, urlopen):
        receipt = {"session_id": "s/1", "workspace_id": "w", "tenant_id": "t", "user_id": "u", "principal_id": "p",
                   "operation_id": "prepare-1", "request_digest": "a" * 64, "status": "unknown",
                       "profile_revision": "rev", "sandbox_id": ""}
        purge = {key: value for key, value in receipt.items() if key != "sandbox_id"}
        purge.update(operation_id="purge-1", status="purged")
        urlopen.side_effect = [Response(b"{}"), Response(json.dumps(receipt).encode()), Response(b"{}"),
                              Response(b"{}"), Response(json.dumps(purge).encode())]
        client = Client("https://sandbox.example")
        client.get_workspace_view("s/1")
        client.prepare_workspace_view("s/1", operation_id="prepare-1", request_digest="a" * 64)
        client.seal_workspace_view("s/1")
        client.get_session_history("s/1")
        client.purge_session_workspace("s/1", operation_id="purge-1", request_digest="a" * 64)
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual([req.method for req in requests], ["GET", "POST", "POST", "GET", "POST"])
        self.assertTrue(all("s%2F1" in req.full_url for req in requests))
        self.assertTrue(requests[2].full_url.endswith("workspace-view?action=seal"))
        self.assertTrue(requests[4].full_url.endswith("workspace:purge"))
        self.assertEqual(json.loads(requests[1].data), {"operation_id": "prepare-1", "request_digest": "a" * 64})

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resolution_requests_facts_and_exec_requests_real_subprocess_policy(self, urlopen):
        urlopen.side_effect = [Response(b"{}"), Response(b"{}")]
        client = Client("https://sandbox.example")
        client.resolve_environment(profile="restricted", include_facts=True)
        client.exec_session_async("session", command=["/usr/bin/python", "x.py"], subprocess_policy="deny")
        self.assertTrue(json.loads(urlopen.call_args_list[0].args[0].data)["include_facts"])
        self.assertEqual(json.loads(urlopen.call_args_list[1].args[0].data)["subprocess_policy"], "deny")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_conditional_delete_transmits_hash_and_checks_receipt_budget(self, urlopen):
        urlopen.return_value = Response(b'{"status":"removed"}')
        client = Client("https://sandbox.example")
        receipt = client.remove_session_file("session", "/workspace/work/a.txt", if_match="a" * 64)
        self.assertEqual(receipt["status"], "removed")
        self.assertEqual(urlopen.call_args.args[0].get_header("If-match"), "a" * 64)


if __name__ == "__main__":
    unittest.main()
