import asyncio
import unittest
from unittest import mock

from genesis_sandbox_client import APIError, ExecRecoveryError, TransportError
from genesis_sandbox_client.async_session import AsyncSandboxSession
from genesis_sandbox_client.types import SandboxOptions


class AsyncExecutionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_refreshes_session_and_runtime_identity(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.resume_session.return_value = {
            "session_id": "session-1",
            "workspace_id": "workspace-1",
            "status": "active",
            "active_sandbox_id": "sandbox-2",
        }
        session = AsyncSandboxSession(client, {"session_id": "session-1"}, SandboxOptions(heartbeat=False))

        result = await session.resume()

        self.assertEqual(result["active_sandbox_id"], "sandbox-2")
        self.assertEqual(session._sandbox_id, "sandbox-2")
        self.assertEqual(session._session["workspace_id"], "workspace-1")
        client.resume_session.assert_called_once_with("session-1")

    async def test_cancel_waiter_cancels_remote_exec(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.return_value = {"exec_id": "exec-1", "status": "running"}
        session = AsyncSandboxSession(client, {"session_id": "sess-1"}, SandboxOptions(heartbeat=False))
        task = asyncio.create_task(session.run_python("print(1)"))
        while not client.exec_session_async.called:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        client.cancel_exec.assert_called_once_with("sess-1", "exec-1", timeout=10, max_retries=1)
        self.assertTrue(client.exec_session_async.call_args.kwargs["operation_id"])

    async def test_async_wait_returns_final_record_output(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.return_value = {"exec_id": "exec-1", "status": "queued"}
        client.get_exec.return_value = {
            "exec_id": "exec-1",
            "status": "succeeded",
            "exit_code": 0,
            "stdout": "hello",
            "stderr": "",
        }
        session = AsyncSandboxSession(client, {"session_id": "sess-1"}, SandboxOptions(heartbeat=False))
        result = await session.run_python("print('hello')")
        self.assertEqual(result.stdout, "hello")

    async def test_interrupted_exec_is_a_terminal_observation(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.return_value = {"exec_id": "exec-interrupted", "status": "running"}
        client.get_exec.return_value = {
            "exec_id": "exec-interrupted",
            "status": "interrupted",
            "error_code": "SERVICE_RESTART_INTERRUPTED",
        }
        session = AsyncSandboxSession(client, {"session_id": "sess-interrupted"}, SandboxOptions(heartbeat=False))

        result = await session.run_python("print(1)")

        self.assertEqual(result.error_code, "SERVICE_RESTART_INTERRUPTED")
        client.get_exec.assert_called_once_with("sess-interrupted", "exec-interrupted")

    async def test_run_retries_transient_observation_error(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.return_value = {"exec_id": "exec-2", "status": "running"}
        client.get_exec.side_effect = [
            TransportError("connection reset"),
            {
                "exec_id": "exec-2",
                "operation_id": "op-2",
                "status": "succeeded",
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
            },
        ]
        session = AsyncSandboxSession(client, {"session_id": "sess-2"}, SandboxOptions(heartbeat=False))

        result = await session.run_python("print('done')", operation_id="op-2")

        self.assertEqual(result.stdout, "done")
        self.assertEqual(client.get_exec.call_count, 2)
        self.assertEqual(client.exec_session_async.call_args.kwargs["operation_id"], "op-2")

    async def test_final_observation_error_preserves_recovery_identity_without_cancel(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.return_value = {"exec_id": "exec-3", "status": "running"}
        client.get_exec.side_effect = APIError(503, "UNAVAILABLE", "temporary outage")
        session = AsyncSandboxSession(client, {"session_id": "sess-3"}, SandboxOptions(heartbeat=False))

        with self.assertRaises(ExecRecoveryError) as caught:
            await session.run_python("print(1)", operation_id="op-3")

        self.assertEqual(caught.exception.session_id, "sess-3")
        self.assertEqual(caught.exception.operation_id, "op-3")
        self.assertEqual(caught.exception.exec_id, "exec-3")
        self.assertEqual(caught.exception.phase, "observe")
        self.assertEqual(client.get_exec.call_count, 3)
        client.cancel_exec.assert_not_called()

    async def test_submit_error_preserves_operation_id(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.side_effect = TransportError("response lost")
        session = AsyncSandboxSession(client, {"session_id": "sess-4"}, SandboxOptions(heartbeat=False))

        with self.assertRaises(ExecRecoveryError) as caught:
            await session.run_python("print(1)", operation_id="op-4")

        self.assertEqual(caught.exception.session_id, "sess-4")
        self.assertEqual(caught.exception.operation_id, "op-4")
        self.assertIsNone(caught.exception.exec_id)
        self.assertEqual(caught.exception.phase, "submit")

    async def test_run_and_run_async_preserve_identity_for_malformed_submit_receipts(self):
        cases = (("run", {}), ("run_async", {"exec_id": "   "}))
        for method_name, receipt in cases:
            with self.subTest(method=method_name, receipt=receipt):
                client = mock.Mock(timeout=1, max_attempts=1)
                client.exec_session_async.return_value = receipt
                session = AsyncSandboxSession(client, {"session_id": "sess-malformed"}, SandboxOptions(heartbeat=False))

                with self.assertRaises(ExecRecoveryError) as caught:
                    await getattr(session, method_name)("print(1)", lang="python", operation_id="op-malformed")

                self.assertEqual(caught.exception.session_id, "sess-malformed")
                self.assertEqual(caught.exception.operation_id, "op-malformed")
                self.assertIsNone(caught.exception.exec_id)
                self.assertEqual(caught.exception.phase, "submit")

    async def test_local_submit_validation_error_is_not_reported_as_recoverable(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.exec_session_async.side_effect = ValueError("invalid local request")
        session = AsyncSandboxSession(client, {"session_id": "sess-invalid"}, SandboxOptions(heartbeat=False))

        with self.assertRaisesRegex(ValueError, "invalid local request"):
            await session.run_async("print(1)", lang="python")

        client.get_exec_by_operation.assert_not_called()

    async def test_operation_lookup_is_available_from_async_session(self):
        record = {"exec_id": "exec-5", "operation_id": "op-5", "status": "running"}
        client = mock.Mock(timeout=1, max_attempts=1)
        client.get_exec_by_operation.return_value = record
        session = AsyncSandboxSession(client, {"session_id": "sess-5"}, SandboxOptions(heartbeat=False))

        result = await session.get_exec_by_operation("op-5")

        self.assertIs(result, record)
        client.get_exec_by_operation.assert_called_once_with("sess-5", "op-5")

    async def test_session_creation_lookup_is_available_from_async_session(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.lookup_session.return_value = {"session_id": "session-lookup"}
        session = AsyncSandboxSession(client, {"session_id": "sess-6"}, SandboxOptions(heartbeat=False))

        result = await session.lookup_session("request-1")

        self.assertEqual(result["session_id"], "session-lookup")
        client.lookup_session.assert_called_once_with("request-1")

    async def test_options_env_goes_to_exec_not_session_create(self):
        # 会话创建协议拒绝持久 env；默认 env 应随 exec 下发并与单次 env 合并。
        from genesis_sandbox_client.async_session import open_sandbox_async

        client = mock.Mock(timeout=1, max_attempts=1)
        client.create_session.return_value = {"session_id": "session-env"}
        session = await open_sandbox_async(client, env={"BASE": "1"}, heartbeat=False)

        self.assertNotIn("env", client.create_session.call_args.kwargs)

        client.exec_session_async.return_value = {"exec_id": "exec-1", "status": "succeeded"}
        await session.run_python("print(1)")
        self.assertEqual(client.exec_session_async.call_args.kwargs["env"], {"BASE": "1"})

        await session.run("print(2)", lang="python", env={"BASE": "2", "EXTRA": "x"})
        self.assertEqual(
            client.exec_session_async.call_args.kwargs["env"],
            {"BASE": "2", "EXTRA": "x"},
        )
        await session.close()
