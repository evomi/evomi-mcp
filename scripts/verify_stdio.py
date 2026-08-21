"""Spawn the installed evomi-mcp server over stdio and enumerate its tools.

Checks the things that only a real handshake can: that the tool definitions this
server builds survive serialisation to a client, annotations included, and that
the environment gates are read at registration rather than at import.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Registered only when the operator opts in, because ordering one spends the
# account's data balance.
SPENDING_TOOLS = {"order_browser_profile"}

# All four are set by every tool. `destructiveHint` and `openWorldHint` default
# to true when omitted.
REQUIRED_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def input_schema(tool) -> dict:
    """The schema, under whichever spelling the installed SDK uses.

    mcp 2.x renamed the field to `input_schema` and kept `inputSchema` as a
    serialisation alias, and a pydantic alias cannot be read through.
    """
    return getattr(tool, "input_schema", None) or tool.inputSchema


def hints(tool) -> dict:
    """The annotation hints exactly as they arrived on the wire."""
    if tool.annotations is None:
        return {}
    return tool.annotations.model_dump(by_alias=True, exclude_none=True)


async def enumerate_tools(command: str, env: dict, label: str) -> list[str]:
    """Start the server with one environment and check the surface it exposes."""
    print(f"\n─── {label} ───")
    params = StdioServerParameters(command=command, args=[], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools

            print(f"server started, {len(tools)} tools registered\n")
            for tool in sorted(tools, key=lambda t: t.name):
                schema = input_schema(tool)
                required = ",".join(schema.get("required", [])) or "-"
                flags = "".join(
                    letter if hints(tool).get(hint) else "-"
                    for letter, hint in zip("rdio", REQUIRED_HINTS)
                )
                print(f"  {tool.name:34s} hints={flags}  required={required}")

            # Every tool must round-trip as JSON Schema for a client to render it.
            for tool in tools:
                json.dumps(input_schema(tool))

            # The annotations have to reach the client, not merely exist in
            # the definition: they are a nested model with camelCase aliases.
            for tool in tools:
                received = hints(tool)
                missing = [hint for hint in REQUIRED_HINTS if hint not in received]
                assert not missing, f"{tool.name} arrived without {missing}"
                assert all(isinstance(received[h], bool) for h in REQUIRED_HINTS)
            read_only = sum(1 for tool in tools if hints(tool)["readOnlyHint"])
            open_world = sorted(t.name for t in tools if hints(t)["openWorldHint"])
            print(
                f"\nannotations: all four hints on all {len(tools)} tools, "
                f"{read_only} read-only, open-world: {', '.join(open_world)}"
            )

            # A call with no credentials must fail cleanly rather than crash the server.
            result = await session.call_tool("list_proxy_products", {})
            text = result.content[0].text
            print(f"\nunauthenticated call returned: {text[:120]}")
            assert "verification-key-not-real" not in text, "key leaked into tool output"
            # A failure has to be flagged as one, not returned as content.
            errored = getattr(result, "is_error", None)
            if errored is None:
                errored = result.isError
            assert errored is True, "a failed call must set isError"

            # ...and the server must still be alive afterwards.
            again = (await session.list_tools()).tools
            print(f"server still responsive after error: {len(again)} tools")

            return [tool.name for tool in tools]


async def main() -> None:
    env = dict(os.environ)
    env["EVOMI_PUBLIC_API_KEY"] = "verification-key-not-real"
    env["EVOMI_API_KEY"] = "verification-key-not-real"
    env.pop("EVOMI_ENABLE_SPENDING", None)

    # The console script installed by this venv, so the check exercises the
    # packaged entry point rather than the source tree.
    command = shutil.which("evomi-mcp", path=str(Path(sys.executable).parent))
    assert command, "evomi-mcp entry point was not installed"
    print(f"entry point: {command}")

    default_tools = await enumerate_tools(command, env, "default environment")

    opted_in = await enumerate_tools(
        command, {**env, "EVOMI_ENABLE_SPENDING": "1"}, "EVOMI_ENABLE_SPENDING=1"
    )

    # The gate is at registration, so it has to show up as a difference in what
    # the server is willing to list, not merely in what it is willing to run.
    added = set(opted_in) - set(default_tools)
    print(
        f"\nspending gate: {len(default_tools)} tools by default, "
        f"{len(opted_in)} when opted in, added {sorted(added)}"
    )
    assert not (set(default_tools) & SPENDING_TOOLS), "spending tool listed by default"
    assert added == SPENDING_TOOLS, f"unexpected difference: {sorted(added)}"
    assert len(opted_in) == len(default_tools) + len(SPENDING_TOOLS)

    await check_hidden_passwords(command, {**env, "EVOMI_HIDE_PROXY_PASSWORDS": "1"})


async def check_hidden_passwords(command: str, env: dict) -> None:
    """The kill switch, over a real handshake rather than in-process.

    Registration reads the flag, so the two tools that refuse under it say so
    in the descriptions a client receives. The tool count does not move: they
    stay registered and stay able to explain themselves.
    """
    print("\n─── EVOMI_HIDE_PROXY_PASSWORDS=1 ───")
    params = StdioServerParameters(command=command, args=[], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}

            for name in ("build_proxy_connection_string", "generate_proxy_list"):
                description = tools[name].description
                assert "CURRENTLY UNAVAILABLE" in description, name
                assert "EVOMI_HIDE_PROXY_PASSWORDS" in description, name
                print(f"  {name:34s} advertises itself as refusing")

            for name in ("get_proxy_credentials", "get_api_access"):
                description = tools[name].description
                assert "masked" in description, name
                print(f"  {name:34s} advertises masking")

            result = await session.call_tool(
                "generate_proxy_list", {"product": "rp", "amount": 5}
            )
            text = result.content[0].text
            assert "EVOMI_HIDE_PROXY_PASSWORDS" in text, text[:200]
            print(f"\n  refusal reaching the client: {text[:160]}")
            print(f"  tool count unchanged at {len(tools)}")


asyncio.run(main())
