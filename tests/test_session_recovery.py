import unittest
from unittest import mock

from genesis_sandbox_client import APIError, ExecRecoveryError, SandboxOptions, TransportError
from genesis_sandbox_client.session import SandboxSession


class SyncSessionRecoveryTests(unittest.TestCase):
    def test_resume_refreshes_session_and_runtime_identity(self):
        client = mock.Mock()
        client.resume_session.return_value = {
            "session_id": "session-1",
            "workspace_id": "workspace-1",
            "status": "active",
            "active_sandbox_id": "sandbox-2",
        }
        session = SandboxSession(client, {"session_id": "session-1"}, SandboxOptions(heartbeat=False))

        result = session.resume()

        self.assertEqual(result["active_sandbox_id"], "sandbox-2")
        self.assertEqual(session._sandbox_id, "sandbox-2")
        self.assertEqual(session._session["workspace_id"], "workspace-1")
        client.resume_session.assert_called_once_with("session-1")

    def test_wait_exec_retries_transient_get_error(self):
        client = mock.Mock()
        client.get_exec.side_effect = [
            TransportError("connection reset"),
            {
                "exec_id": "exec-1",
                "operation_id": "op-1",
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
            },
        ]
        session = SandboxSession(client, {"session_id": "session-1"}, SandboxOptions(heartbeat=False))

        result = session.wait_exec("exec-1", poll_interval=0.001, max_wait=1, operation_id="op-1")

        self.assertEqual(result.stdout, "done")
        self.assertEqual(client.get_exec.call_count, 2)

    def test_interrupted_exec_is_a_terminal_observation(self):
        client = mock.Mock()
        client.get_exec.return_value = {
            "exec_id": "exec-interrupted",
            "operation_id": "op-interrupted",
            "status": "interrupted",
            "error_code": "SERVICE_RESTART_INTERRUPTED",
        }
        session = SandboxSession(client, {"session_id": "session-interrupted"}, SandboxOptions(heartbeat=False))

        result = session.wait_exec("exec-interrupted", poll_interval=0.001, max_wait=1)

        self.assertEqual(result.error_code, "SERVICE_RESTART_INTERRUPTED")
        client.get_exec.assert_called_once_with("session-interrupted", "exec-interrupted")

    def test_final_wait_error_preserves_ids_without_cancelling(self):
        client = mock.Mock()
        client.get_exec.side_effect = TransportError("connection reset")
        session = SandboxSession(client, {"session_id": "session-2"}, SandboxOptions(heartbeat=False))

        with self.assertRaises(ExecRecoveryError) as caught:
            session.wait_exec("exec-2", poll_interval=0.001, max_wait=1, operation_id="op-2")

        self.assertEqual(caught.exception.session_id, "session-2")
        self.assertEqual(caught.exception.operation_id, "op-2")
        self.assertEqual(caught.exception.exec_id, "exec-2")
        self.assertEqual(caught.exception.phase, "observe")
        self.assertEqual(client.get_exec.call_count, 3)
        client.cancel_exec.assert_not_called()

    def test_operation_lookup_is_exposed_on_session(self):
        record = {"exec_id": "exec-3", "operation_id": "op-3", "status": "running"}
        client = mock.Mock()
        client.get_exec_by_operation.return_value = record
        session = SandboxSession(client, {"session_id": "session-3"}, SandboxOptions(heartbeat=False))

        self.assertIs(session.get_exec_by_operation("op-3"), record)
        client.get_exec_by_operation.assert_called_once_with("session-3", "op-3")

    def test_submit_failure_exposes_operation_id(self):
        client = mock.Mock()
        client.exec_named_session.side_effect = TransportError("response lost")
        session = SandboxSession(client, {"session_id": "session-4"}, SandboxOptions(heartbeat=False))

        with self.assertRaises(ExecRecoveryError) as caught:
            session.run_python("print(1)", operation_id="op-4")

        self.assertEqual(caught.exception.session_id, "session-4")
        self.assertEqual(caught.exception.operation_id, "op-4")
        self.assertIsNone(caught.exception.exec_id)
        self.assertEqual(caught.exception.phase, "submit")

    def test_local_submit_validation_error_is_not_reported_as_recoverable(self):
        client = mock.Mock()
        client.exec_named_session.side_effect = ValueError("invalid local request")
        session = SandboxSession(client, {"session_id": "session-5"}, SandboxOptions(heartbeat=False))

        with self.assertRaisesRegex(ValueError, "invalid local request"):
            session.run_python("print(1)")

    def test_definite_client_error_is_not_reported_as_recoverable(self):
        client = mock.Mock()
        error = APIError(400, "INVALID_ARGUMENT", "invalid command")
        client.exec_named_session.side_effect = error
        session = SandboxSession(client, {"session_id": "session-6"}, SandboxOptions(heartbeat=False))

        with self.assertRaises(APIError) as caught:
            session.run_python("print(1)")

        self.assertIs(caught.exception, error)

    def test_async_submit_rejects_missing_or_blank_exec_id_with_recovery_identity(self):
        for receipt in ({}, {"exec_id": "  "}):
            with self.subTest(receipt=receipt):
                client = mock.Mock()
                client.exec_session_async.return_value = receipt
                session = SandboxSession(client, {"session_id": "session-malformed"}, SandboxOptions(heartbeat=False))

                with self.assertRaises(ExecRecoveryError) as caught:
                    session.run_async("print(1)", lang="python", operation_id="op-malformed")

                self.assertEqual(caught.exception.session_id, "session-malformed")
                self.assertEqual(caught.exception.operation_id, "op-malformed")
                self.assertIsNone(caught.exception.exec_id)
                self.assertEqual(caught.exception.phase, "submit")

    def test_session_lookup_wrapper_delegates(self):
        client = mock.Mock()
        client.lookup_session.return_value = {"session_id": "session-lookup"}
        session = SandboxSession(client, {"session_id": "session-3"}, SandboxOptions(heartbeat=False))

        result = session.lookup_session("request-1")

        self.assertEqual(result["session_id"], "session-lookup")
        client.lookup_session.assert_called_once_with("request-1")

    def test_options_env_goes_to_exec_not_session_create(self):
        # 会话创建协议拒绝持久 env；默认 env 应随 exec 下发并与单次 env 合并。
        from genesis_sandbox_client.session import new_sandbox

        client = mock.Mock()
        client.create_session.return_value = {"session_id": "session-env"}
        session = new_sandbox(client, env={"BASE": "1"}, heartbeat=False)

        self.assertNotIn("env", client.create_session.call_args.kwargs)

        client.exec_named_session.return_value = {"exec_id": "exec-1", "status": "succeeded"}
        session.run_python("print(1)")
        self.assertEqual(client.exec_named_session.call_args.kwargs["env"], {"BASE": "1"})

        session.run("print(2)", lang="python", env={"BASE": "2", "EXTRA": "x"})
        self.assertEqual(
            client.exec_named_session.call_args.kwargs["env"],
            {"BASE": "2", "EXTRA": "x"},
        )


if __name__ == "__main__":
    unittest.main()
