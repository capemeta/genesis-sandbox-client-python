"""classify_error 稳定分类映射（供 Adapter 转通用契约错误码）。"""

import json
import unittest

from genesis_sandbox_client import (
    APIError,
    ExecRecoveryError,
    ProtocolError,
    SandboxError,
    TransportError,
    classify_error,
)


class ClassifyErrorTests(unittest.TestCase):
    def test_api_status_mapping(self):
        cases = (
            (400, "invalid_request"),
            (422, "invalid_request"),
            (401, "unauthorized"),
            (403, "unauthorized"),
            (404, "not_found"),
            (409, "conflict"),
            (412, "conflict"),
            (429, "rate_limited"),
            (500, "transient"),
            (503, "transient"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(classify_error(APIError(status, "CODE", "msg")), expected)

    def test_redirect_style_status_is_protocol(self):
        self.assertEqual(classify_error(APIError(302, "", "Found")), "protocol")

    def test_transport_error_maps_to_transport(self):
        self.assertEqual(classify_error(TransportError("connection reset")), "transport")

    def test_protocol_error_maps_to_protocol(self):
        self.assertEqual(classify_error(ProtocolError("bad json")), "protocol")

    def test_json_decode_failure_maps_to_protocol(self):
        try:
            json.loads("<html>")
        except ValueError as exc:
            self.assertEqual(classify_error(exc), "protocol")

    def test_local_validation_value_error_maps_to_invalid_request(self):
        self.assertEqual(classify_error(ValueError("exactly one of code or command is required")), "invalid_request")

    def test_recovery_error_classifies_by_cause(self):
        error = ExecRecoveryError("sess-1", "op-1", phase="submit", cause=TransportError("response lost"))
        self.assertEqual(classify_error(error), "transport")

    def test_recovery_error_without_cause_is_unknown(self):
        error = ExecRecoveryError("sess-1", "op-1", phase="lookup")
        self.assertEqual(classify_error(error), "unknown")

    def test_unknown_exception_is_unknown(self):
        self.assertEqual(classify_error(SandboxError("mystery")), "unknown")
        self.assertEqual(classify_error(RuntimeError("mystery")), "unknown")

    def test_categories_are_stable_strings(self):
        allowed = {
            "invalid_request",
            "unauthorized",
            "not_found",
            "conflict",
            "rate_limited",
            "transient",
            "transport",
            "protocol",
            "unknown",
        }
        samples = [
            APIError(400, "", ""),
            APIError(401, "", ""),
            APIError(404, "", ""),
            APIError(409, "", ""),
            APIError(429, "", ""),
            APIError(500, "", ""),
            TransportError(""),
            ProtocolError(""),
            ValueError(""),
            RuntimeError(""),
        ]
        for exc in samples:
            self.assertIn(classify_error(exc), allowed)


if __name__ == "__main__":
    unittest.main()
