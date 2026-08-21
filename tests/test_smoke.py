"""Smoke tests: the package must import and register its handlers.

mcp 1.x and 2.x register request handlers differently — 1.x through the
`@server.list_tools()` / `@server.call_tool()` decorators, 2.x through `on_*`
constructor kwargs — so importing the server module and inspecting what it
registered is a test in its own right.

server.py speaks both majors, so these tests assert against whichever is
installed and fail loudly if the shim picked the wrong branch. Run them against
both (CI does) — passing on one proves nothing about the other.
"""

import inspect
import re
from importlib import metadata

import pytest

import evomi_mcp
from evomi_mcp import proxy_tools
from evomi_mcp import server as server_module
from evomi_mcp.server import ToolError


def _mcp_major() -> int:
    return int(metadata.version("mcp").split(".")[0])


def _is_error(result) -> bool:
    """Read the flag under either spelling.

    mcp 2.x renamed the field to `is_error`, keeping `isError` as the wire alias.
    An alias can be constructed with but not read through, so server.py can pass
    `isError=` to both majors while a reader has to ask.
    """
    return result.is_error if hasattr(result, "is_error") else result.isError


def test_sdk_branch_matches_installed_mcp():
    """The shim must not silently take the 1.x path against mcp 2.x, or vice versa.

    Everything else here passes either way, so without this an environment that
    resolved the other major would look healthy while exercising dead code.
    """
    assert server_module._MCP_2X is (_mcp_major() >= 2)


def test_server_module_imports_and_registers_handlers():
    if server_module._MCP_2X:
        # 2.x dispatches by method string and dropped the public handler dicts.
        assert server_module.server.get_request_handler("tools/list") is not None
        assert server_module.server.get_request_handler("tools/call") is not None
    else:
        import mcp.types as mcp_types

        assert mcp_types.ListToolsRequest in server_module.server.request_handlers
        assert mcp_types.CallToolRequest in server_module.server.request_handlers


def test_console_script_target_is_callable():
    assert callable(server_module.main)


def test_declared_version_matches_package_metadata():
    assert evomi_mcp.__version__ == metadata.version("evomi-mcp")


def test_server_reports_its_own_version_not_the_sdks():
    """`serverInfo.version` must be Evomi's version.

    `Server(...)` falls back to the SDK's own version when none is passed, so
    this pins that a client asking which evomi-mcp it is talking to is told the
    evomi-mcp version.
    """
    assert server_module.server.version == evomi_mcp.__version__
    assert server_module.server.version != metadata.version("mcp")


async def test_advertised_tools_are_well_formed():
    tools = await server_module.list_tools()
    assert tools, "list_tools() returned no tools"

    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), "duplicate tool names advertised"

    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        # mcp 2.x renamed the attribute to `input_schema`; `inputSchema` stayed
        # as the wire alias, so read it through the same accessor server.py uses.
        assert server_module._input_schema(tool)["type"] == "object"


def _dispatched_names(dispatcher) -> set[str]:
    """The tool names a dispatcher branches on, read out of its source."""
    return set(re.findall(r'name == "([^"]+)"', inspect.getsource(dispatcher)))


async def test_every_advertised_tool_has_a_dispatch_branch(monkeypatch):
    """Nothing is advertised without a handler, and nothing handled goes unadvertised.

    Two dispatchers have to be read, not one. The scraper tools are an
    `elif name == "..."` chain in `call_tool`; the Public API tools are routed
    out of `call_tool` by a `PROXY_TOOL_NAMES` membership test before that chain
    is reached, and pick their branch inside `handle_proxy_tool`. Reading only
    `call_tool` finds none of them and reports all ten as never dispatched.

    `PROXY_TOOL_NAMES` is deliberately not what counts as dispatched here. It is
    built from the same definitions that produce the advertisement, so unioning
    it in would compare the tool table against itself: a tool whose handler was
    never written would pass. The branches in `handle_proxy_tool` are the
    dispatch, so those are what is read.

    Run with the spending gate open so the one gated tool is advertised too, and
    the two sets can be compared exactly in both directions with nothing
    exempted.
    """
    monkeypatch.setenv("EVOMI_ENABLE_SPENDING", "1")

    advertised = {tool.name for tool in await server_module.list_tools()}
    dispatched = _dispatched_names(server_module.call_tool) | _dispatched_names(
        proxy_tools.handle_proxy_tool
    )

    assert not advertised - dispatched, "advertised but never dispatched"
    assert not dispatched - advertised, "dispatched but never advertised"


async def test_every_proxy_tool_actually_reaches_a_handler(monkeypatch, stub):
    """The source read above, confirmed by running the dispatcher.

    A regex proves a branch is written, not that it is reachable for the name
    that is advertised. So call `handle_proxy_tool` with every advertised proxy
    tool name and require that none of them falls through to its "Unknown proxy
    tool" guard. The arguments are empty on purpose: a handler that rejects them
    has still been reached, and routing is what is under test here, not any
    handler's behaviour.
    """
    monkeypatch.setenv("EVOMI_ENABLE_SPENDING", "1")
    stub()

    advertised = {tool.name for tool in await server_module.list_tools()}
    proxy_names = advertised & set(proxy_tools.PROXY_TOOL_NAMES)
    assert proxy_names, "no Public API tools advertised, so this asserts nothing"

    for name in sorted(proxy_names):
        try:
            await proxy_tools.handle_proxy_tool(name, {})
        except Exception as e:
            assert not str(e).startswith("Unknown proxy tool"), (
                f"{name} is advertised but has no branch in handle_proxy_tool"
            )


class _StubClient:
    """Stands in for EvomiClient so tool dispatch runs without credentials."""

    def __init__(self, exc: Exception | None = None):
        self._exc = exc

    async def get_account_info(self):
        if self._exc:
            raise self._exc
        return {"credits": 42}


@pytest.fixture
def stub_client(monkeypatch):
    """Install a stub API client, so dispatch runs without reaching the API."""

    def _install(exc: Exception | None = None):
        monkeypatch.setattr(server_module, "_client", _StubClient(exc))
        return server_module._client

    return _install


async def test_tool_failure_is_reported_as_an_error_result(stub_client):
    """A failing tool must set `isError`, not return "Error: ..." as a success.

    A message returned as ordinary `TextContent` is a successful call whose body
    happens to read "Error", and nothing in the protocol says otherwise.
    """
    stub_client(RuntimeError("boom"))

    result = await server_module.tool_result("get_account_info", {})

    assert _is_error(result) is True
    assert result.content[0].text == "Error: boom"


async def test_successful_tool_call_is_not_flagged_as_an_error(stub_client):
    stub_client()

    result = await server_module.tool_result("get_account_info", {})

    assert _is_error(result) is False
    assert "42" in result.content[0].text


async def test_unknown_tool_is_reported_as_an_error_result(stub_client):
    stub_client()

    result = await server_module.tool_result("no_such_tool", {})

    assert _is_error(result) is True
    assert result.content[0].text == "Unknown tool: no_such_tool"


async def test_missing_required_argument_is_rejected_before_dispatch(stub_client):
    """mcp 1.x validated arguments in its decorator; 2.x does not, so we do.

    Without the check a missing required argument reaches the dispatch and
    surfaces as a bare `KeyError: 'url'`.
    """
    stub_client()

    result = await server_module.tool_result("scrape_url", {})

    assert _is_error(result) is True
    assert result.content[0].text == "Input validation error: 'url' is a required property"


async def test_dispatch_raises_rather_than_returning_error_text(stub_client):
    """The contract `tool_result` relies on: failures leave `call_tool` as raises.

    If a future branch returns "Error: ..." content from the dispatch instead,
    `isError` silently stops being set.
    """
    stub_client(RuntimeError("boom"))

    with pytest.raises(ToolError, match="Error: boom"):
        await server_module.call_tool("get_account_info", {})
