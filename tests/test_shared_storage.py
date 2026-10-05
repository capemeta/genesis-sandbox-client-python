import io
import json
import unittest
from unittest import mock

from genesis_sandbox_client import Client


class Response(io.BytesIO):
    status = 200


class SharedStorageTests(unittest.TestCase):
    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_shared_inspection_and_creation_use_registered_refs_only(self, urlopen):
        urlopen.side_effect = [Response(b"{}"), Response(b"{}")]
        client = Client("https://sandbox.example", token="protected")
        client.inspect_shared_storage_resource("execution-workspaces", "resource")
        client.create_workspace("workspace", workspace_binding={"mode": "shared", "storage_ref": "execution-workspaces",
                                                                "resource_id": "resource", "binding_version": 1})
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertTrue(requests[0].full_url.endswith("/v1/storage-resources/execution-workspaces/resource"))
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer protected")
        binding = json.loads(requests[1].data)["workspace_binding"]
        self.assertEqual(binding["binding_version"], 1)
        self.assertNotIn("host_path", binding)

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_binding_validation_before_transport(self, urlopen):
        client = Client("https://sandbox.example")
        for binding in [{"mode": "auto"}, {"mode": "shared"}, {"mode": "isolated", "storage_ref": "x"},
                        {"mode": "shared", "storage_ref": "x", "resource_id": "r", "binding_version": True},
                        {"mode": "shared", "storage_ref": "x", "resource_id": "r", "binding_version": 9007199254740992},
                        {"mode": "shared", "storage_ref": "x", "resource_id": "r", "binding_version": 1,
                            "host_path": "/srv"}]:
            with self.assertRaises(ValueError):
                client.create_workspace("workspace", workspace_binding=binding)
        urlopen.assert_not_called()

    @mock.patch("genesis_sandbox_client.client._urlopen")
    def test_executable_mutation_is_conditional_and_not_retried(self, urlopen):
        urlopen.return_value = Response(b'{"executable":true}')
        client = Client("https://sandbox.example", token="protected")
        result = client.set_session_file_executable("session", "work/run.sh", True,
                                                    if_match="revision", expected_executable=False)
        self.assertTrue(result["executable"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.get_header("If-match"), "revision")
        self.assertEqual(json.loads(request.data), {"executable": True, "expected_executable": False})
        self.assertEqual(urlopen.call_count, 1)
        with self.assertRaises(ValueError):
            client.set_session_file_executable("session", "work/run.sh", 1)
