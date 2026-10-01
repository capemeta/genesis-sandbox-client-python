"""read_exec_logs 预算分页、游标不透明与保留缺口显式标记。"""

import unittest
from unittest import mock

from genesis_sandbox_client import Client, ProtocolError
from genesis_sandbox_client.async_session import AsyncSandboxSession
from genesis_sandbox_client.session import SandboxSession
from genesis_sandbox_client.types import SandboxOptions


def _sse_lines(*events):
    lines = []
    for event_id, stream, message in events:
        lines.append(f"id: {event_id}\n")
        lines.append(f"event: {stream}\n")
        # data 载荷即计费字节；保持与消息等长便于断言预算。
        lines.append(f"data: {message}\n")
        lines.append("\n")
    return lines


class _StreamResponse:
    def __init__(self, lines):
        self.status = 200
        self.lines = [line.encode("utf-8") for line in lines]
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def close(self):
        self.closed = True


class ReadExecLogsTests(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_budget_stops_early_and_cursor_is_opaque(self, urlopen):
        urlopen.return_value = _StreamResponse(
            _sse_lines(
                (1, "stdout", "aaaaaaaaaa"),
                (2, "stdout", "bbbbbbbbbb"),
                (3, "stdout", "cccccccccc"),
            )
        )
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", byte_budget=25, max_events=10)

        self.assertEqual(len(page["events"]), 2)
        self.assertEqual(page["next_cursor"], "2")
        self.assertFalse(page["exhausted"])
        self.assertFalse(page["gap_detected"])
        self.assertIsInstance(page["next_cursor"], str)
        self.assertIsInstance(page["events"][0]["cursor"], str)
        self.assertEqual(page["events"][0]["bytes"], 10)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_max_events_budget(self, urlopen):
        urlopen.return_value = _StreamResponse(
            _sse_lines(
                (1, "stdout", "a"),
                (2, "stdout", "b"),
                (3, "stdout", "c"),
            )
        )
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", byte_budget=1 << 20, max_events=1)

        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["next_cursor"], "1")
        self.assertFalse(page["exhausted"])

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_stream_end_marks_exhausted(self, urlopen):
        urlopen.return_value = _StreamResponse(_sse_lines((1, "stdout", "a")))
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", byte_budget=1 << 20, max_events=10)

        self.assertTrue(page["exhausted"])
        self.assertEqual(page["next_cursor"], "1")
        self.assertFalse(page["gap_detected"])

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_gap_in_cursor_is_flagged_not_skipped(self, urlopen):
        urlopen.return_value = _StreamResponse(
            _sse_lines(
                (1, "stdout", "a"),
                (2, "stdout", "b"),
                (5, "stdout", "c"),
            )
        )
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", byte_budget=1 << 20, max_events=10)

        self.assertTrue(page["gap_detected"])
        # 缺口事件不被静默丢弃：3 条都返回，缺口仅显式标记。
        self.assertEqual(len(page["events"]), 3)
        self.assertEqual(page["next_cursor"], "5")

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resume_cursor_gap_is_flagged(self, urlopen):
        urlopen.return_value = _StreamResponse(_sse_lines((7, "stdout", "a")))
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", cursor="5", byte_budget=1 << 20, max_events=10)

        self.assertTrue(page["gap_detected"])
        self.assertEqual(page["next_cursor"], "7")
        request = urlopen.call_args.args[0]
        self.assertIn("cursor=5", request.full_url)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_resume_after_partial_page_is_contiguous(self, urlopen):
        urlopen.return_value = _StreamResponse(_sse_lines((3, "stdout", "c"), (4, "stdout", "d")))
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", cursor="2", byte_budget=1 << 20, max_events=10)

        self.assertFalse(page["gap_detected"])
        self.assertEqual([e["cursor"] for e in page["events"]], ["3", "4"])

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_single_oversized_event_still_progresses(self, urlopen):
        urlopen.return_value = _StreamResponse(_sse_lines((1, "stdout", "x" * 50)))
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", byte_budget=1, max_events=10)

        self.assertEqual(len(page["events"]), 1)
        self.assertTrue(page["exhausted"])

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_empty_stream_returns_start_cursor(self, urlopen):
        urlopen.return_value = _StreamResponse([])
        client = Client("http://127.0.0.1:18010")

        page = client.read_exec_logs("sess-1", "exec-1", byte_budget=1 << 20, max_events=10)

        self.assertEqual(page, {"events": [], "next_cursor": "0", "exhausted": True, "gap_detected": False})

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_invalid_budgets_rejected(self, urlopen):
        client = Client("http://127.0.0.1:18010")
        with self.assertRaises(ValueError):
            client.read_exec_logs("sess-1", "exec-1", cursor=None, byte_budget=0, max_events=1)
        with self.assertRaises(ValueError):
            client.read_exec_logs("sess-1", "exec-1", cursor=None, byte_budget=1, max_events=0)
        urlopen.assert_not_called()

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_event_without_cursor_is_protocol_error(self, urlopen):
        response = _StreamResponse(["event: stdout\n", "data: {}\n", "\n"])
        urlopen.return_value = response
        client = Client("http://127.0.0.1:18010")
        with self.assertRaises(ProtocolError):
            client.read_exec_logs("sess-1", "exec-1", byte_budget=1 << 20, max_events=10)


class SessionLogHelperTests(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_sync_session_log_wrappers(self, urlopen):
        urlopen.return_value = _StreamResponse(_sse_lines((1, "stdout", "a")))
        client = Client("http://127.0.0.1:18010")
        session = SandboxSession(client, {"session_id": "sess-1"}, SandboxOptions(heartbeat=False))

        page = session.read_exec_logs("exec-1", byte_budget=1 << 20, max_events=10)
        self.assertEqual(page["next_cursor"], "1")

        events = list(session.stream_exec_logs("exec-1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "stdout")


class AsyncSessionLogHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_session_log_wrappers(self):
        client = mock.Mock(timeout=1, max_attempts=1)
        client.read_exec_logs.return_value = {
            "events": [{"cursor": "1", "stream": "stdout", "data": "a", "bytes": 1}],
            "next_cursor": "1",
            "exhausted": True,
            "gap_detected": False,
        }
        client.stream_exec_logs.return_value = iter([])
        session = AsyncSandboxSession(client, {"session_id": "sess-1"}, SandboxOptions(heartbeat=False))

        page = await session.read_exec_logs("exec-1", byte_budget=1024, max_events=10)
        self.assertEqual(page["next_cursor"], "1")
        client.read_exec_logs.assert_called_once_with("sess-1", "exec-1", cursor=None, byte_budget=1024, max_events=10)

        events = [event async for event in session.stream_exec_logs("exec-1")]
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
