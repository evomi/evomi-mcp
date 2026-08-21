"""End-to-end handshake against the real entry point over stdio.

Deliberately speaks raw JSON-RPC to a subprocess instead of using the SDK's own
client: that is what an MCP client does, it exercises `main()` and the console
script rather than the module's internals, and it stays readable across SDK
majors that disagree about what a client object looks like.

This is the test that catches a registration break: a handler registration that
stops working leaves an importable module whose `tools/list` is empty or errors.
"""

import asyncio
import json
import pathlib
import subprocess
import sys
from importlib import metadata

import pytest

import evomi_mcp
from evomi_mcp import server as server_module

# The handshake era both majors serve. mcp 2.x also serves the 2026-07-28
# envelope era, which has no `initialize` at all; `test_modern_era_*` covers it.
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 60


class Client:
    """A minimal JSON-RPC-over-stdio client for the server subprocess."""

    def __init__(self, argv: list[str]):
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def request(self, id_: int, method: str, params: dict | None = None) -> dict:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        message = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            message["params"] = params
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise AssertionError(f"server closed stdout without answering {method}: {self.stderr()}")
        return json.loads(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self._proc.stdin is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def initialize(self) -> dict:
        response = self.request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "evomi-mcp-tests", "version": "0"},
            },
        )
        self.notify("notifications/initialized", {})
        return response

    def stderr(self) -> str:
        return self._proc.stderr.read() if self._proc.stderr else ""

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        try:
            self._proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self._proc.kill()
            raise AssertionError("server did not exit after stdin closed")


@pytest.fixture(params=["module", "console-script"])
def client(request):
    """Both documented ways to start the server must handshake identically."""
    if request.param == "module":
        argv = [sys.executable, "-m", "evomi_mcp.server"]
    else:
        script = pathlib.Path(sys.executable).with_name("evomi-mcp")
        if not script.exists():  # pragma: no cover
            pytest.skip("console script not installed in this environment")
        argv = [str(script)]

    client = Client(argv)
    yield client
    client.close()


def test_initialize_reports_evomi_version(client):
    result = client.initialize()["result"]

    assert result["serverInfo"]["name"] == "evomi-mcp"
    assert result["serverInfo"]["version"] == evomi_mcp.__version__
    # `Server(...)` falls back to the SDK's own version when none is passed.
    assert result["serverInfo"]["version"] != metadata.version("mcp")
    assert result["capabilities"]["tools"] is not None


def test_tools_list_advertises_every_tool_over_the_wire(client):
    client.initialize()
    tools = client.request(2, "tools/list", {})["result"]["tools"]

    # Asserted against the module rather than a hardcoded count, so a branch
    # that adds tools does not have to edit this test to stay honest.
    expected = asyncio.run(server_module.list_tools())

    assert [tool["name"] for tool in tools] == [tool.name for tool in expected]
    assert len(tools) >= 30, f"expected at least the 30 shipped tools, got {len(tools)}"

    for advertised, declared in zip(tools, expected):
        assert advertised["description"] == declared.description
        assert advertised["inputSchema"] == server_module._input_schema(declared)


def test_tool_errors_are_flagged_over_the_wire(client):
    """A failing call must come back as `isError`, not as a success.

    No credentials are needed to prove it: argument validation rejects before
    the tool runs, and a bare environment cannot build an API client at all.
    """
    client.initialize()
    result = client.request(3, "tools/call", {"name": "scrape_url", "arguments": {}})["result"]

    assert result["isError"] is True
    assert result["content"][0]["text"] == "Input validation error: 'url' is a required property"


def test_unknown_method_is_a_protocol_error(client):
    """Errors that are not a tool's fault stay protocol errors, not tool results."""
    client.initialize()
    response = client.request(4, "resources/list", {})

    assert "error" in response, response
    assert response["error"]["code"] == -32601


@pytest.mark.skipif(
    int(metadata.version("mcp").split(".")[0]) < 2,
    reason="the 2026-07-28 envelope era is only served by mcp 2.x",
)
def test_modern_era_serves_tools_without_a_handshake():
    """mcp 2.x also serves the era that replaced `initialize` with per-request `_meta`."""
    client = Client([sys.executable, "-m", "evomi_mcp.server"])
    try:
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "evomi-mcp-tests", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        result = client.request(1, "tools/list", {"_meta": meta})["result"]

        assert len(result["tools"]) >= 30
        stamp = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        assert stamp == {"name": "evomi-mcp", "version": evomi_mcp.__version__}
    finally:
        client.close()
