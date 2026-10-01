"""文件下载在源头遵守明确字节预算。"""

import io
import unittest
from unittest.mock import patch

from genesis_sandbox_client import Client, ProtocolError


class SessionFileBudgetTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("https://sandbox.example", token="test")

    def test_exact_budget_preserves_content(self):
        with patch.object(self.client, "_raw_request", return_value=io.BytesIO(b"abc")) as transport:
            self.assertEqual(self.client.download_session_file("s/id", "work/a", max_bytes=3), b"abc")
        self.assertIn("s%2Fid", transport.call_args.args[1])

    def test_limit_reads_only_one_extra_byte_and_closes(self):
        class CountingResponse(io.BytesIO):
            requested = []

            def read(self, size=-1):
                self.requested.append(size)
                return super().read(size)

        response = CountingResponse(b"x" * 100)
        with patch.object(self.client, "_raw_request", return_value=response):
            with self.assertRaises(ProtocolError):
                self.client.download_session_file("s", "work/a", max_bytes=3)
        self.assertEqual(response.requested, [4])
        self.assertTrue(response.closed)

    def test_invalid_budget_has_no_transport_side_effect(self):
        for budget in (True, 0, -1, "10", 16777217):
            with patch.object(self.client, "_raw_request") as transport:
                with self.assertRaises(ValueError):
                    self.client.download_session_file("s", "a", max_bytes=budget)
                transport.assert_not_called()
