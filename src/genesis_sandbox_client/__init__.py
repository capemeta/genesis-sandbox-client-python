"""Genesis Sandbox Python SDK — dependency-free client with session helpers.

Public API::

    from genesis_sandbox_client import Client, new_sandbox, new_sandbox_async, quick_python, quick_run

Sync session usage::

    with new_sandbox(client, profile="python") as sb:
        r = sb.run_python('print("hello")')

Async session usage::

    async with new_sandbox_async(client, profile="python") as sb:
        r = await sb.run_python('print("hello")')

Quick one-shot job (no session)::

    result = quick_python(client, 'print(42)')
"""

from .async_session import AsyncSandboxSession, new_sandbox_async, open_sandbox_async
from .client import Client
from .control_probe import RuntimeControlCapabilities
from .errors import APIError, ExecRecoveryError, ProtocolError, SandboxError, TransportError, classify_error
from .session import SandboxSession, new_sandbox, quick_python, quick_run
from .types import CancelReceipt, EffectiveEnvironment, ExecResult, SandboxOptions, SSEEvent

__version__ = "0.3.0"

__all__ = [
    # HTTP Client
    "Client",
    "RuntimeControlCapabilities",
    "SandboxError",
    "APIError",
    "TransportError",
    "ProtocolError",
    "ExecRecoveryError",
    "classify_error",
    # Common types
    "CancelReceipt",
    "ExecResult",
    "EffectiveEnvironment",
    "SandboxOptions",
    "SSEEvent",
    # Sync
    "SandboxSession",
    "new_sandbox",
    # Async
    "AsyncSandboxSession",
    "new_sandbox_async",
    "open_sandbox_async",
    # Quick helpers (Job mode)
    "quick_run",
    "quick_python",
]
