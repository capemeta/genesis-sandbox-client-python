"""取消语义：接受 vs 停止确认 vs 停止未知（ExecRecoveryError phase="cancel"）。"""

import unittest
from unittest import mock

from genesis_sandbox_client import APIError, CancelReceipt, ExecRecoveryError, TransportError
from genesis_sandbox_client.async_session import AsyncSandboxSession
from genesis_sandbox_client.session import SandboxSession
from genesis_sandbox_client.types import SandboxOptions


class CancelReceiptMappingTests(unittest.TestCase):
    def test_cancelled_status_is_stop_confirmed(self):
        receipt = CancelReceipt.from_record(
            "exec-1", {"exec_id": "exec-1", "status": "cancelled", "stop_confirmed": True}
        )
        self.assertEqual(receipt.outcome, "stop_confirmed")
        self.assertTrue(receipt.stop_confirmed)

    def test_timed_out_status_is_stop_confirmed(self):
        receipt = CancelReceipt.from_record(
            "exec-1", {"exec_id": "exec-1", "status": "timed_out", "stop_confirmed": True}
        )
        self.assertEqual(receipt.outcome, "stop_confirmed")
        self.assertTrue(receipt.stop_confirmed)

    def test_execution_finishing_first_wins_as_already_terminal(self):
        for status in ("succeeded", "failed"):
            with self.subTest(status=status):
                receipt = CancelReceipt.from_record(
                    "exec-1", {"exec_id": "exec-1", "status": status, "stop_confirmed": True}
                )
                self.assertEqual(receipt.outcome, "already_terminal")
                self.assertTrue(receipt.stop_confirmed)

    def test_interrupted_is_terminal_but_stop_unconfirmed(self):
        receipt = CancelReceipt.from_record("exec-1", {"exec_id": "exec-1", "status": "interrupted"})
        self.assertEqual(receipt.outcome, "stop_unknown")
        self.assertFalse(receipt.stop_confirmed)

    def test_non_terminal_record_is_only_accepted(self):
        receipt = CancelReceipt.from_record("exec-1", {"exec_id": "exec-1", "status": "running"})
        self.assertEqual(receipt.outcome, "accepted")
        self.assertFalse(receipt.stop_confirmed)


class SyncCancelReceiptTests(unittest.TestCase):
    def _session(self, client):
        return SandboxSession(client, {"session_id": "sess-1"}, SandboxOptions(heartbeat=False))

    def test_stop_confirmed_receipt(self):
        client = mock.Mock()
        client.cancel_exec.return_value = {"exec_id": "exec-1", "status": "cancelled", "stop_confirmed": True}
        receipt = self._session(client).cancel_exec_receipt("exec-1")
        self.assertEqual(receipt.outcome, "stop_confirmed")
        client.cancel_exec.assert_called_once_with("sess-1", "exec-1")

    def test_stop_unknown_raises_recovery_error_with_cancel_phase(self):
        client = mock.Mock()
        client.cancel_exec.side_effect = TransportError("connection reset")
        session = self._session(client)

        with self.assertRaises(ExecRecoveryError) as caught:
            session.cancel_exec_receipt("exec-1", operation_id="op-1")

        self.assertEqual(caught.exception.phase, "cancel")
        self.assertEqual(caught.exception.session_id, "sess-1")
        self.assertEqual(caught.exception.operation_id, "op-1")
        self.assertEqual(caught.exception.exec_id, "exec-1")
        self.assertIsInstance(caught.exception.cause, TransportError)

    def test_unconfirmed_stop_api_error_is_stop_unknown(self):
        # 服务端 5 秒内联等待超时：以错误表达“停止未确认”。
        client = mock.Mock()
        client.cancel_exec.side_effect = APIError(503, "RUNTIME_UNAVAILABLE", "execution stop was not confirmed")
        session = self._session(client)

        with self.assertRaises(ExecRecoveryError) as caught:
            session.cancel_exec_receipt("exec-1")

        self.assertEqual(caught.exception.phase, "cancel")

    def test_operation_id_falls_back_to_tracked_identity(self):
        client = mock.Mock()
        client.exec_session_async.return_value = {"exec_id": "exec-9", "status": "running"}
        client.cancel_exec.side_effect = APIError(503, "RUNTIME_UNAVAILABLE", "stop unconfirmed")
        session = self._session(client)
        session.run_async("print(1)", lang="python", operation_id="op-9")

        with self.assertRaises(ExecRecoveryError) as caught:
            session.cancel_exec_receipt("exec-9")

        self.assertEqual(caught.exception.operation_id, "op-9")


class AsyncCancelReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_unknown_raises_recovery_error_with_cancel_phase(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.cancel_exec.side_effect = APIError(503, "RUNTIME_UNAVAILABLE", "execution stop was not confirmed")
        session = AsyncSandboxSession(client, {"session_id": "sess-2"}, SandboxOptions(heartbeat=False))

        with self.assertRaises(ExecRecoveryError) as caught:
            await session.cancel_exec_receipt("exec-2", operation_id="op-2")

        self.assertEqual(caught.exception.phase, "cancel")
        self.assertEqual(caught.exception.operation_id, "op-2")
        self.assertEqual(caught.exception.exec_id, "exec-2")

    async def test_stop_confirmed_receipt(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.cancel_exec.return_value = {"exec_id": "exec-3", "status": "cancelled", "stop_confirmed": True}
        session = AsyncSandboxSession(client, {"session_id": "sess-3"}, SandboxOptions(heartbeat=False))

        receipt = await session.cancel_exec_receipt("exec-3")

        self.assertEqual(receipt.outcome, "stop_confirmed")
        self.assertIsInstance(receipt, CancelReceipt)


if __name__ == "__main__":
    unittest.main()
