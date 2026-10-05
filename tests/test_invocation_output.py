import json
from unittest.mock import patch

import pytest
from test_workspace_control import Response

from genesis_sandbox_client import Client
from genesis_sandbox_client.invocation_output import output_request


@pytest.mark.parametrize("value", ["", "output/..", "output/a/b", "/workspace/output/a", "output/a\n", "work/a"])
def test_output_path_rejected_before_transport(value):
    with patch("genesis_sandbox_client.client._urlopen") as transport:
        with pytest.raises(ValueError):
            Client("https://example.test").exec_session_async("s", command=["python"], output_directory=value)
        transport.assert_not_called()


@pytest.mark.parametrize("method", ["exec_named_session", "exec_session_async"])
def test_original_request_contains_output_identity(method):
    with patch("genesis_sandbox_client.client._urlopen", return_value=Response(b"{}")) as transport:
        getattr(Client("https://example.test"), method)("s", command=["python"],
            operation_id="original", output_directory="output/invocation-1")
        assert json.loads(transport.call_args.args[0].data)["output_directory"] == "output/invocation-1"
    assert output_request(None) == {}
