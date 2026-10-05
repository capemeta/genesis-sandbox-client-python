import copy
import hashlib
import json
import unittest
from unittest import mock

from genesis_sandbox_client import Client
from genesis_sandbox_client.errors import ProtocolError
from genesis_sandbox_client.execution_governance import maintenance_proof, trusted_governance


def original_governance():
    return {
        "context": {"tenant_id": "tenant", "user_id": "user", "run_id": "run", "trace_id": "trace",
                    "authorization_scope": "execution:write", "decision_reference": None,
                    "subject_kind": "enterprise", "invocation_id": "invocation",
                    "execution_id": "execution", "attempt_id": "attempt"},
        "lease_ref": {"provider_id": "provider", "lease_id": "lease",
                      "workspace": {"workspace_key": "workspace", "provider_id": "provider",
                                    "resource_id": "workspace", "scope": "execution:write", "generation": 1},
                      "generation": 1, "expires_at": "2026-10-04T00:00:00Z"},
        "platform_operation_id": "platform-operation", "request_digest": "a" * 64,
        "source_identity": "b" * 64, "workspace_id": "workspace", "authorized_work_write": True,
    }


def query_and_proof():
    governance = original_governance()
    query = {"context": governance["context"], "operation_id": "maintenance-operation",
             "workspace_id": "workspace", "source_identity": "b" * 64}
    query["claim_digest"] = hashlib.sha256(json.dumps(query, sort_keys=True, separators=(",", ":"),
                                                    ensure_ascii=False).encode()).hexdigest()
    proof = {key: query[key] for key in ("context", "operation_id", "claim_digest", "source_identity")}
    proof.update(root_device=1, root_inode=2, calls=[{
        "call_id": "original-call", "context": governance["context"], "lease_ref": governance["lease_ref"],
        "operation_id": governance["platform_operation_id"], "request_digest": "a" * 64,
        "execution_id": "execution", "output_directory": "output/original-call", "uid": 100001,
        "gid": 100001, "root_device": 1, "root_inode": 3, "authorized_work_write": True,
        "stop_confirmed": True}], leaves=[{
            "path": path, "device": 1, "inode": inode, "uid": 0, "gid": 65532,
            "mode": 0o1770, "directory": True, "call_id": None,
        } for path, inode in (("work", 4), ("output", 5))])
    proof["leaves"].append({"path": "output/original-call", "device": 1, "inode": 3,
        "uid": 0, "gid": 100001, "mode": 0o770, "directory": True, "call_id": "original-call"})
    return query, proof


class ExecutionGovernanceTests(unittest.TestCase):
    def test_original_governance_exact_identity_and_boolean(self):
        original = original_governance()
        self.assertEqual(trusted_governance(original), original)
        for mutation in (
            lambda g: g.update(authorized_work_write=1),
            lambda g: g.update(authorized_work_write=False),
            lambda g: g["context"].update(trace_id="trace\u2028separator"),
            lambda g: g["context"].update(trace_id="trace\u2029separator"),
            lambda g: g["context"].update(execution_id=None),
            lambda g: g["lease_ref"].update(generation=True),
            lambda g: g["lease_ref"]["workspace"].update(scope="foreign-scope"),
            lambda g: g.update(host_path="/fake"),
        ):
            invalid = copy.deepcopy(original)
            mutation(invalid)
            with self.assertRaises(ValueError):
                trusted_governance(invalid)

    def test_exec_transmits_governance_once_without_mutating_input(self):
        client = Client("https://fixture.invalid")
        client._request = mock.Mock(return_value={"exec_id": "original"})
        original = original_governance()
        client.exec_session_async("session", command=["true"], operation_id="native-original",
                                  trusted_governance=original)
        self.assertEqual(client._request.call_count, 1)
        self.assertEqual(client._request.call_args.args[2]["trusted_governance"], original)
        with self.assertRaises(ValueError):
            client.exec_session_async("session", command=["true"], trusted_governance=original)
        self.assertEqual(client._request.call_count, 1)

    def test_maintenance_query_exact_scoped_receipt(self):
        query, proof = query_and_proof()
        client = Client("https://fixture.invalid")
        client._request = mock.Mock(return_value=proof)
        result = client.query_workspace_maintenance_proof("workspace", context=query["context"],
                   operation_id=query["operation_id"], source_identity=query["source_identity"],
                   claim_digest=query["claim_digest"])
        self.assertEqual(result, proof)
        self.assertEqual(client._request.call_args.args,
                         ("POST", "/v1/workspaces/workspace/maintenance-proof:query", query))
        self.assertEqual(client._request.call_count, 1)

    def test_file_api_only_source_allows_no_command_calls(self):
        query, proof = query_and_proof()
        proof["calls"] = []
        proof["leaves"] = proof["leaves"][:2]
        self.assertEqual(maintenance_proof(proof, query), proof)
        proof["leaves"][0]["call_id"] = "unknown-original"
        with self.assertRaises(ProtocolError):
            maintenance_proof(proof, query)

    def test_maintenance_unknown_stop_scope_tuple_and_paths_rejected(self):
        query, proof = query_and_proof()
        for mutation in (
            lambda p: p["calls"][0].update(stop_confirmed=False),
            lambda p: p["calls"][0].update(execution_id="different-execution"),
            lambda p: p["calls"][0]["context"].update(invocation_id=None, execution_id=None, attempt_id=None),
            lambda p: p["calls"][0]["context"].update(user_id="foreign"),
            lambda p: p["calls"][0]["lease_ref"]["workspace"].update(scope="foreign-scope"),
            lambda p: p["leaves"][0].update(device=True),
            lambda p: p["leaves"][0].update(path="work/../payload"),
            lambda p: p["leaves"].pop(),
            lambda p: p["calls"].append(copy.deepcopy(p["calls"][0])),
            lambda p: p["leaves"][0].update(call_id="unknown-call"),
            lambda p: p["leaves"][0].update(call_id="original-call", uid=200001),
            lambda p: p["leaves"][0].update(uid=200001),
            lambda p: p["leaves"][0].update(path="output/unregistered", uid=0, gid=100001, call_id=None),
            lambda p: p.update(root_device=2**63),
        ):
            invalid = copy.deepcopy(proof)
            mutation(invalid)
            with self.assertRaises(ProtocolError):
                maintenance_proof(invalid, query)
