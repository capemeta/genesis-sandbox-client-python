import io
import json
import unittest
from datetime import UTC, datetime
from unittest import mock

from genesis_sandbox_client import Client
from genesis_sandbox_client.errors import ProtocolError, TransportError
from genesis_sandbox_client.workspace_lifecycle import workspace_lifecycle_request_digest


class Response(io.BytesIO):
    status = 200


def receipt(operation="original", state="paused", hold_seconds=86400):
    value = {"operation_id": operation, "workspace_id": "w/1", "revision": 1,
             "state": state, "deadline": "2026-10-02T00:00:00Z"}
    if state == "terminal":
        value["terminal_at"] = "2026-10-01T00:00:00Z"
    value["request_digest"] = workspace_lifecycle_request_digest({"operation_id": operation, "expected_revision": 0,
        "state": state, "hold_seconds": 0 if state == "terminal" else hold_seconds,
        "terminal_at": value.get("terminal_at"), "retention_seconds": 86400 if state == "terminal" else 0})
    return value


class WorkspaceLifecycleTests(unittest.TestCase):
    def test_cross_language_digest_vectors(self):
        self.assertEqual(workspace_lifecycle_request_digest({"operation_id": "original", "expected_revision": 0,
            "state": "active"}), "600867eb32528c3e0e110f9de2ec17e7e8c96b73428a54545934953a05b49442")
        self.assertEqual(workspace_lifecycle_request_digest({"operation_id": "terminal", "expected_revision": 7,
            "state": "terminal", "terminal_at": "2026-10-01T08:30:12.123456+08:00", "retention_seconds": 86400}),
            "abb14a40cc3f77bc822599e2a4b089677e01c3b66ff178513f3e71e84adba74a")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_control_and_original_lookup(self, urlopen):
        urlopen.side_effect = [Response(json.dumps(receipt()).encode()),
                               Response(json.dumps({"state": "found", "receipt": receipt()}).encode())]
        client = Client("https://sandbox.example")
        client.control_workspace_lifecycle("w/1", "original", 0, "paused", hold_seconds=86400)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/w%2F1/lifecycle"))
        self.assertEqual(json.loads(request.data), {"operation_id": "original", "expected_revision": 0,
                                                  "state": "paused", "hold_seconds": 86400})
        self.assertEqual(client.lookup_workspace_lifecycle("w/1", "original")["state"], "found")
        self.assertEqual(urlopen.call_args.args[0].method, "GET")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_unknown_mutation_is_not_retried(self, urlopen):
        urlopen.side_effect = OSError("connection response lost")
        client = Client("https://sandbox.example", max_attempts=3)
        with self.assertRaises(TransportError):
            client.control_workspace_lifecycle("w/1", "original", 0, "active")
        self.assertEqual(urlopen.call_count, 1)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_validation_and_history_are_fail_closed(self, urlopen):
        client = Client("https://sandbox.example")
        for kwargs in [{"expected_revision": True}, {"hold_seconds": 86401}, {"operation_id": "../bad"},
                       {"retention_seconds": 1}, {"terminal_at": datetime.now()}]:
            arguments = {"workspace_id": "w/1", "operation_id": "original", "expected_revision": 0, "state": "active"}
            arguments.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.control_workspace_lifecycle(**arguments)
        self.assertFalse(urlopen.called)
        urlopen.return_value = Response(b'{"state":"history_expired","receipt":null}')
        self.assertEqual(client.lookup_workspace_lifecycle("w/1", "original")["state"], "history_expired")
        urlopen.return_value = Response(json.dumps({"state": "found", "receipt": receipt("other")}).encode())
        with self.assertRaises(ProtocolError):
            client.lookup_workspace_lifecycle("w/1", "original")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_terminal_uses_authoritative_parent_time(self, urlopen):
        urlopen.return_value = Response(json.dumps(receipt(state="terminal")).encode())
        client = Client("https://sandbox.example")
        client.control_workspace_lifecycle("w/1", "original", 0, "terminal",
                                           terminal_at=datetime(2026, 10, 1, tzinfo=UTC), retention_seconds=86400)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)["terminal_at"], "2026-10-01T00:00:00Z")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_correct_digest_does_not_authorize_changed_terminal_or_deadline(self, urlopen):
        client = Client("https://sandbox.example")
        for field, value in [("terminal_at", "2026-10-01T00:00:01Z"),
                             ("deadline", "2026-10-02T00:00:01Z"),
                             ("deadline", "2026-10-02"), ("deadline", "2026-10-02T00:00:00.000000001Z")]:
            forged = {**receipt(state="terminal"), field: value}
            urlopen.return_value = Response(json.dumps(forged).encode())
            with self.subTest(field=field, value=value), self.assertRaises(ProtocolError):
                client.control_workspace_lifecycle("w/1", "original", 0, "terminal",
                    terminal_at=datetime(2026, 10, 1, tzinfo=UTC), retention_seconds=86400)

    def test_digest_rejects_extra_fields_boolean_and_invalid_calendar(self):
        base = {"operation_id": "original", "expected_revision": 0, "state": "active"}
        for changed in [{"host_path": "/host"}, {"expected_revision": True},
                        {"state": "terminal", "retention_seconds": 86400, "terminal_at": "2026-02-30T00:00:00Z"},
                        {"state": "terminal", "retention_seconds": 86400,
                            "terminal_at": "2026-10-01T00:00:00.123456789Z"}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                workspace_lifecycle_request_digest({**base, **changed})
