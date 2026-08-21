"""The MCP surface itself: every tool is enumerable and has a usable schema.

Schemas are read through `server._input_schema`, never as `tool.inputSchema`.
mcp 2.x renamed the field to `input_schema` and left `inputSchema` as a wire
alias, and a pydantic alias can be constructed with but not read through, so the
attribute read raises there. Same for the annotation hints, via
`server._tool_annotations`.
"""

import os

import pytest

from evomi_mcp.proxy import PRODUCT_CODES
from evomi_mcp.proxy_tools import (
    PROXY_TOOL_NAMES,
    echo_suppression,
    proxy_tool_definitions,
    spending_tool_definitions,
)
from evomi_mcp.server import _input_schema, _tool_annotations, list_tools

EXPECTED_PROXY_TOOLS = {
    "list_proxy_products",
    "get_proxy_credentials",
    "build_proxy_connection_string",
    "generate_proxy_list",
    "get_proxy_usage",
    "list_proxy_targeting_options",
    "rotate_proxy_session",
    "get_api_access",
    "list_browser_profiles",
}

# Registered only when EVOMI_ENABLE_SPENDING opts in, because calling them costs
# the customer money.
EXPECTED_SPENDING_TOOLS = {"order_browser_profile"}

def _all_tool_definitions():
    """Both halves of the proxy surface, built with the ambient flags cleared.

    This runs at import time to parametrise the checks below, which is before any
    fixture has had a chance to normalise the environment. Both flags now change
    what it would return — `EVOMI_ENABLE_SPENDING` changes which tools exist and
    `EVOMI_HIDE_PROXY_PASSWORDS` changes their descriptions — so neither is left
    to whatever the suite happened to be started with.
    """
    saved = {
        name: os.environ.pop(name, None)
        for name in ("EVOMI_HIDE_PROXY_PASSWORDS", "EVOMI_ENABLE_SPENDING")
    }
    try:
        return list(
            {
                tool.name: tool
                for tool in (*proxy_tool_definitions(), *spending_tool_definitions())
            }.values()
        )
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


# The parametrised checks below must cover the gated tools whatever the ambient
# environment says, so both halves are collected and de-duplicated by name.
ALL_TOOL_DEFINITIONS = _all_tool_definitions()

# Tools whose output contains a live credential must say so, so a user can
# reason about what their assistant will see.
CREDENTIAL_TOOLS = {
    "get_proxy_credentials",
    "build_proxy_connection_string",
    "generate_proxy_list",
}


async def test_every_tool_is_enumerable():
    names = [tool.name for tool in await list_tools()]

    assert EXPECTED_PROXY_TOOLS.issubset(names)
    assert "scrape_url" in names, "the existing scraper surface must remain registered"
    assert len(names) == len(set(names)), "tool names must be unique"


def test_definitions_match_the_dispatch_table():
    assert PROXY_TOOL_NAMES == EXPECTED_PROXY_TOOLS | EXPECTED_SPENDING_TOOLS


# ─── Spending gate ──────────────────────────────────────────────────────────────


async def test_spending_tools_are_not_offered_by_default(monkeypatch):
    monkeypatch.delenv("EVOMI_ENABLE_SPENDING", raising=False)
    names = {tool.name for tool in await list_tools()}

    assert not (names & EXPECTED_SPENDING_TOOLS), (
        "a tool that spends the customer's balance must not be listed unless the "
        "operator opted in"
    )


async def test_spending_tools_are_offered_once_opted_in(monkeypatch):
    monkeypatch.setenv("EVOMI_ENABLE_SPENDING", "1")
    names = {tool.name for tool in await list_tools()}

    assert EXPECTED_SPENDING_TOOLS.issubset(names)


async def test_opting_in_adds_only_the_spending_tools(monkeypatch):
    monkeypatch.delenv("EVOMI_ENABLE_SPENDING", raising=False)
    without = [tool.name for tool in await list_tools()]

    monkeypatch.setenv("EVOMI_ENABLE_SPENDING", "1")
    with_spending = [tool.name for tool in await list_tools()]

    assert set(with_spending) - set(without) == EXPECTED_SPENDING_TOOLS
    assert len(with_spending) == len(without) + len(EXPECTED_SPENDING_TOOLS)
    assert len(with_spending) == len(set(with_spending))


@pytest.mark.parametrize("tool", spending_tool_definitions(), ids=lambda t: t.name)
def test_spending_tools_state_the_cost(tool):
    assert "1.5 GB" in tool.description, "the price must be in the description"
    assert "free" in tool.description, "the free allowance must be in the description"


@pytest.mark.parametrize("tool", ALL_TOOL_DEFINITIONS, ids=lambda t: t.name)
def test_schema_is_well_formed(tool):
    schema = _input_schema(tool)

    assert schema["type"] == "object"
    assert isinstance(schema.get("properties"), dict)

    for name in schema.get("required", []):
        assert name in schema["properties"], f"required '{name}' is not a declared property"

    for name, spec in schema["properties"].items():
        assert "type" in spec, f"property '{name}' has no type"
        if "default" in spec and "enum" in spec:
            assert spec["default"] in spec["enum"], f"property '{name}' defaults outside its enum"


@pytest.mark.parametrize("tool", ALL_TOOL_DEFINITIONS, ids=lambda t: t.name)
def test_description_is_written_for_a_model(tool):
    assert tool.description
    assert len(tool.description) > 80, "too terse for a model to route on"


@pytest.mark.parametrize("tool", ALL_TOOL_DEFINITIONS, ids=lambda t: t.name)
def test_credential_disclosure_is_declared(tool):
    disclosed = "PASSWORD" in tool.description or "credential" in tool.description.lower()

    if tool.name in CREDENTIAL_TOOLS:
        assert disclosed, "a tool returning credentials must say so in its description"


def test_mutating_tools_are_labelled():
    """The freeform MUTATING prefix and the annotations must agree.

    Annotations only reach a client that reads them; the description reaches
    every model. Exactly the tools whose hints are not read-only carry the
    prefix.
    """
    mutating = [
        tool for tool in ALL_TOOL_DEFINITIONS if tool.description.startswith("MUTATING")
    ]

    assert {tool.name for tool in mutating} == {
        "rotate_proxy_session",
        "order_browser_profile",
    }
    assert {
        tool.name
        for tool in ALL_TOOL_DEFINITIONS
        if not _tool_annotations(tool)["readOnlyHint"]
    } == {tool.name for tool in mutating}


@pytest.mark.parametrize(
    "tool",
    [t for t in ALL_TOOL_DEFINITIONS if "product" in _input_schema(t)["properties"]],
    ids=lambda t: t.name,
)
def test_product_enums_stay_within_the_catalog(tool):
    enum = _input_schema(tool)["properties"]["product"].get("enum", [])

    assert enum, "product must be constrained to known codes"
    assert set(enum).issubset(set(PRODUCT_CODES))


# ─── Echo suppression ───────────────────────────────────────────────────────────

# The MCP spec has no annotation for "this result is sensitive", so a tool that
# hands back a live credential can only say so in prose. The convention for this
# is not masking but an instruction not to repeat the value, which targets the
# failure that happens: an assistant restating the password into a transcript.
ECHO_SUPPRESSED_TOOLS = {
    "get_proxy_credentials",
    "build_proxy_connection_string",
    "generate_proxy_list",
    "get_api_access",
}


@pytest.mark.parametrize("tool", ALL_TOOL_DEFINITIONS, ids=lambda t: t.name)
def test_only_the_credential_tools_carry_echo_suppression(tool):
    suppressed = "SECRET HANDLING" in tool.description

    assert suppressed is (tool.name in ECHO_SUPPRESSED_TOOLS), (
        "every tool that returns a live secret must carry the wording, and no "
        "tool that does not should spend description budget on it"
    )


@pytest.mark.parametrize("name", sorted(ECHO_SUPPRESSED_TOOLS))
def test_the_wording_is_the_same_instruction_everywhere(name):
    """One helper builds all four, so they cannot drift apart in wording.

    Only the noun differs, which is why the assertion is against the parts of
    the sentence that carry the instruction rather than the whole string.
    """
    tool = next(t for t in ALL_TOOL_DEFINITIONS if t.name == name)

    for fragment in (
        "is a live credential",
        "do not repeat the value back in your reply",
        "only if the user explicitly asks to see it",
    ):
        assert fragment in tool.description, f"{name} is missing '{fragment}'"


def test_echo_suppression_stays_short_enough_to_not_crowd_the_description():
    """It has to be read alongside the functional half, not instead of it."""
    wording = echo_suppression("password")

    assert len(wording) < 400
    for tool in ALL_TOOL_DEFINITIONS:
        if tool.name in ECHO_SUPPRESSED_TOOLS:
            assert len(wording) < len(tool.description) - len(wording), (
                f"{tool.name}: the secret-handling note is over half the description"
            )


# ─── Annotations ────────────────────────────────────────────────────────────────

# `destructiveHint` and `openWorldHint` both default to true in the spec, so a
# tool that omits its annotations advertises itself as potentially destructive
# and open-world. Every tool has to state all four, which is why this is checked
# across the whole surface rather than per tool.
REQUIRED_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

# The tools whose reachable world is not Evomi's own API. Everything else on this
# surface talks to a fixed set of Evomi endpoints and nothing else.
OPEN_WORLD_TOOLS = {
    "scrape_url",
    "crawl_website",
    "map_website",
    "search_domains",
    "agent_request",
    # A schema created or updated with `test` set is validated against the page
    # it describes, and a config generated from a prompt is designed against the
    # site it is for. The fetch is Evomi-side, which is equally true of
    # scrape_url, so the same reasoning has to reach the same answer.
    "create_schema",
    "update_schema",
    "generate_config",
}


async def test_every_tool_states_all_four_hints():
    tools = await list_tools()

    for tool in tools:
        hints = _tool_annotations(tool)
        missing = [hint for hint in REQUIRED_HINTS if hint not in hints]
        assert not missing, f"{tool.name} leaves {missing} to the spec default"


async def test_no_read_only_tool_is_advertised_as_destructive():
    """Omitting the hints would advertise every lookup as potentially destructive."""
    for tool in await list_tools():
        hints = _tool_annotations(tool)
        if hints["readOnlyHint"]:
            assert hints["destructiveHint"] is False, tool.name


async def test_open_world_is_set_only_where_the_target_is_unbounded():
    advertised = {
        tool.name
        for tool in await list_tools()
        if _tool_annotations(tool)["openWorldHint"]
    }

    assert advertised == OPEN_WORLD_TOOLS


@pytest.mark.parametrize(
    "name, expected",
    [
        # Plain lookups against Evomi's own API.
        ("list_proxy_products", (True, False, True, False)),
        ("get_proxy_credentials", (True, False, True, False)),
        ("get_proxy_usage", (True, False, True, False)),
        ("list_configs", (True, False, True, False)),
        # Read-only, but the value is freshly minted: a session id generated for
        # this call means two identical calls return different strings.
        ("build_proxy_connection_string", (True, False, False, False)),
        ("generate_proxy_list", (True, False, False, False)),
        # Fetches a caller-chosen URL. Still read-only — nothing the user owns
        # changes — but not idempotent, because the page can move, credits are
        # spent per call, and a storage config makes each call write an object.
        ("scrape_url", (True, False, False, True)),
        # Additive: creates something, leaves nothing altered, and doing it twice
        # leaves two of them.
        ("create_config", (False, False, False, False)),
        # Replaces the contents of an existing entity, so destructive, and
        # idempotent because reapplying it lands on the same state.
        ("update_config", (False, True, True, False)),
        ("delete_config", (False, True, True, False)),
        # Flipping a flag twice returns it to where it started.
        ("toggle_schedule", (False, False, False, False)),
        # Releases a live exit IP that cannot be asked for back, dropping
        # whatever is connected through it.
        ("rotate_proxy_session", (False, True, False, False)),
        # A pass-through to an Evomi-side agent that can do anything the rest of
        # this surface can, including deleting saved configs.
        ("agent_request", (False, True, False, True)),
    ],
)
async def test_the_hints_chosen_per_tool(name, expected):
    tool = next(t for t in await list_tools() if t.name == name)
    hints = _tool_annotations(tool)

    assert tuple(hints[hint] for hint in REQUIRED_HINTS) == expected


def test_the_billable_tool_is_additive_rather_than_destructive():
    """Spending money is not a `destructiveHint`, and saying so is the honest read.

    The spec's "destructive" is about replacing or removing something that
    existed. Ordering a profile only adds one. No hint in the spec means "costs
    money", which is why that warning lives in the description and in the
    registration gate.
    """
    [tool] = spending_tool_definitions()
    hints = _tool_annotations(tool)

    assert hints == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert "SPENDS THE ACCOUNT'S BALANCE" in tool.description
