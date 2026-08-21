"""EVOMI_HIDE_PROXY_PASSWORDS: what happens under the flag, tool by tool.

With the flag set, no tool returns a live credential. Tools whose entire output
is connection strings refuse; the rest answer with the secret masked, because a
connection string with a masked password cannot connect.

The audit at the bottom of this file is what keeps that guarantee whole. It is
driven off the registered tool list rather than a hand-written one, so a tool
added later cannot quietly opt out of it: with no entry in `TOOL_ARGUMENTS` it
fails collection, and with one it has to prove that nothing the stubbed API
handed it comes back out.
"""

import json

import pytest

from evomi_mcp.proxy_tools import (
    MAX_PROXIES_PER_CALL,
    handle_proxy_tool,
    proxy_tool_definitions,
    spending_tool_definitions,
)
from evomi_mcp.security import MASK
from evomi_mcp.server import _input_schema

# Every secret the stubbed API below hands to a handler. If any of these reaches
# a tool's output while the flag is set, the kill switch leaks.
#
# `test-public-key-0000` is the configured Public API key from conftest: it is
# never in a response body, only in error text an API might echo, and it is
# included so the audit covers that route too.
CONFIGURED_SECRETS = (
    "rp_secret_pw",
    "rpc_secret_pw",
    "mp_secret_pw",
    "dcp_secret_pw",
    "static_pw_1",
    "static_pw_2",
    "scraper_key_abc123",
    "browser_key_def456",
    "test-public-key-0000",
)

ACCESS = {
    "scraper": {
        "has_access": True,
        "credits": 847.5,
        "concurrency": 5,
        "api_key": "scraper_key_abc123",
        "endpoint_url": "https://scrape.evomi.com/api/v1",
    },
    "browser": {
        "has_access": True,
        "credits": 10,
        "concurrency": 1,
        "api_key": "browser_key_def456",
        "max_session_length_seconds": 600,
        "endpoint_url": "wss://browser.evomi.com",
    },
}

USAGE = {
    "data": {"bandwidth": {"products": [{"totalBandwidth": 12.0, "stats": {}}]}},
    "meta": {"period": "3d", "granularity": "day", "total_bandwidth_mb": 12.0},
}

SETTINGS = {"data": {"rp": {"countries": {"US": "United States"}}}}

# One line of what GET /public/generate really returns: the account password is
# in it, so a handler that passes the body through leaks whether or not it
# understands the format.
GENERATED = "http://acct_user:rp_secret_pw@premium-residential.evomi.com:1000\n"

# Arguments that reach each tool's real work rather than an early validation
# error, since an argument error would pass the audit without proving anything.
# Keyed by tool name and checked for completeness against the registered list.
TOOL_ARGUMENTS: dict[str, dict] = {
    "list_proxy_products": {},
    "get_proxy_credentials": {"product": "rp"},
    "build_proxy_connection_string": {"product": "rp", "count": 3, "session": "sticky"},
    "generate_proxy_list": {"product": "rp", "amount": 5},
    "get_proxy_usage": {"product": "rp"},
    "list_proxy_targeting_options": {"product": "rp"},
    "rotate_proxy_session": {"product": "rp", "session_id": "ABC123"},
    # The most aggressive form on purpose: an explicit request to reveal.
    "get_api_access": {"include_api_key": True},
    "list_browser_profiles": {},
    "order_browser_profile": {"os": "windows", "browser_version": "120"},
    # Static Residential goes down a separate branch in three of the handlers
    # above, so it is audited as its own case rather than folded into "rp".
    "get_proxy_credentials[static]": {"product": "static_residential"},
    "build_proxy_connection_string[static]": {"product": "static_residential"},
}

ALL_TOOL_NAMES = frozenset(
    tool.name for tool in (*proxy_tool_definitions(), *spending_tool_definitions())
)

# Tools that refuse outright under the flag, rather than answering with the
# credential masked out. See the module docstring in proxy_tools for why these
# two and not the others: everything they return is a connection string, and a
# connection string with a masked password cannot connect — it can only look as
# though it should.
REFUSING_TOOLS = {"build_proxy_connection_string", "generate_proxy_list"}


@pytest.fixture
def leaky_api(stub, products_payload):
    """A stubbed Public API whose every response carries a secret."""
    return stub(
        products_payload,
        generate=GENERATED,
        usage=USAGE,
        settings=SETTINGS,
        profiles={"profiles": [{"id": "prof_1", "os": "windows"}]},
        **ACCESS,
    )


def _secrets_in(text: str) -> list[str]:
    return [secret for secret in CONFIGURED_SECRETS if secret in text]


# ─── The audit ──────────────────────────────────────────────────────────────────


def test_every_registered_tool_is_covered_by_the_audit():
    """A tool added later must not be able to skip the leak check by omission."""
    audited = {name.split("[")[0] for name in TOOL_ARGUMENTS}

    assert audited == set(ALL_TOOL_NAMES), (
        "TOOL_ARGUMENTS and the registered tool list disagree; add the new tool "
        "to TOOL_ARGUMENTS so the flag is checked against it"
    )


@pytest.mark.parametrize("case", sorted(TOOL_ARGUMENTS), ids=lambda case: case)
async def test_no_configured_secret_escapes_while_passwords_are_hidden(
    leaky_api, monkeypatch, case
):
    """No secret in any tool's output — or in its error — under the flag.

    Both outcomes are acceptable and both are checked the same way: a handler
    may answer with the secret masked, or refuse. What it may not do is put the
    value anywhere a client can read it, which includes nested fields, the curl
    example, and the text of whatever it raises.
    """
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")
    monkeypatch.setenv("EVOMI_ENABLE_SPENDING", "1")
    name = case.split("[")[0]

    try:
        output = await handle_proxy_tool(name, TOOL_ARGUMENTS[case])
    except Exception as e:
        output = f"{type(e).__name__}: {e}"

    leaked = _secrets_in(output)
    assert not leaked, f"{case} leaked {leaked} with EVOMI_HIDE_PROXY_PASSWORDS set"


@pytest.mark.parametrize("case", sorted(TOOL_ARGUMENTS), ids=lambda case: case)
async def test_the_audit_would_catch_a_leak(leaky_api, case):
    """The same tools, flag unset, to prove the assertion above can fail.

    Without this the audit passes just as well against a handler that returns
    nothing at all, or one that refuses for an unrelated reason.
    """
    name = case.split("[")[0]
    if name in {"get_proxy_usage", "list_proxy_targeting_options", "rotate_proxy_session",
                "list_browser_profiles", "order_browser_profile", "list_proxy_products"}:
        pytest.skip("returns no credential either way, so it cannot demonstrate a leak")

    try:
        output = await handle_proxy_tool(name, TOOL_ARGUMENTS[case])
    except Exception as e:  # pragma: no cover - would be a real failure
        pytest.fail(f"{case} should succeed with the flag unset: {e}")

    assert _secrets_in(output), (
        f"{case} returned no secret even with the flag unset, so its entry in "
        "the audit proves nothing"
    )


# ─── Per-tool behaviour under the flag ──────────────────────────────────────────


@pytest.mark.parametrize("tool", sorted(REFUSING_TOOLS))
async def test_refusal_names_the_variable_that_caused_it(leaky_api, monkeypatch, tool):
    """The refusal has to be actionable, which means naming the flag.

    A bare "unavailable" leaves the user thinking the tool is broken. Naming
    `EVOMI_HIDE_PROXY_PASSWORDS` lets them ask the operator, and pointing at the
    credential-free tools lets them get on with the part that still works.
    """
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")

    with pytest.raises(Exception) as excinfo:
        await handle_proxy_tool(tool, TOOL_ARGUMENTS[tool])

    message = str(excinfo.value)
    assert "EVOMI_HIDE_PROXY_PASSWORDS" in message
    assert tool in message
    assert "list_proxy_products" in message


async def test_generation_is_refused_before_the_api_is_asked(leaky_api, monkeypatch):
    """Nothing should mint bulk credentials that are then thrown away."""
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")

    with pytest.raises(Exception):
        await handle_proxy_tool("generate_proxy_list", {"product": "rp", "amount": 5})

    assert leaky_api.calls == [], "the refusal must come before the request"


async def test_credentials_still_answer_with_the_password_masked(leaky_api, monkeypatch):
    """`get_proxy_credentials` masks rather than refusing.

    Everything else in its response — username, endpoint, ports, balance — is
    useful without the password, and a masked value still answers "does this
    account have a credential for this product".
    """
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")

    result = json.loads(await handle_proxy_tool("get_proxy_credentials", {"product": "rp"}))

    assert result["passwords_hidden"] is True
    assert result["password"] == MASK
    assert result["username"] == "acct_user"
    assert result["endpoint"] == "premium-residential.evomi.com"


async def test_static_residential_ip_passwords_are_masked_individually(
    leaky_api, monkeypatch
):
    """The nested case: the passwords here are one per rented IP, inside a list."""
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")

    result = json.loads(
        await handle_proxy_tool("get_proxy_credentials", {"product": "static_residential"})
    )

    assert [ip["password"] for ip in result["ips"]] == [MASK, MASK]
    assert [ip["ip"] for ip in result["ips"]] == ["203.0.113.10", "203.0.113.11"]


async def test_service_keys_stay_masked_against_an_explicit_reveal(leaky_api, monkeypatch):
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")

    result = json.loads(
        await handle_proxy_tool("get_api_access", {"include_api_key": True})
    )

    assert result["scraper"]["api_key_revealed"] is False
    assert result["browser"]["api_key_revealed"] is False
    assert result["scraper"]["credits"] == 847.5, "the useful half must survive"


# ─── What the model is told ─────────────────────────────────────────────────────


def _tool(name: str):
    return next(
        tool
        for tool in (*proxy_tool_definitions(), *spending_tool_definitions())
        if tool.name == name
    )


@pytest.mark.parametrize("tool", sorted(REFUSING_TOOLS))
def test_a_refusing_tool_says_so_in_its_description(monkeypatch, tool):
    """Registration reads the flag, so the description can be honest about it.

    `proxy_tool_definitions()` runs on every `tools/list`, which means a model
    can be told up front that the call will refuse instead of spending a round
    trip discovering it.
    """
    monkeypatch.delenv("EVOMI_HIDE_PROXY_PASSWORDS", raising=False)
    assert "CURRENTLY UNAVAILABLE" not in _tool(tool).description

    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")
    description = _tool(tool).description

    assert "CURRENTLY UNAVAILABLE" in description
    assert "EVOMI_HIDE_PROXY_PASSWORDS" in description


@pytest.mark.parametrize(
    "tool", ["get_proxy_credentials", "get_api_access"]
)
def test_a_masking_tool_says_it_masks(monkeypatch, tool):
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")
    description = _tool(tool).description

    assert "masked" in description
    assert "EVOMI_HIDE_PROXY_PASSWORDS" in description
    assert "CURRENTLY UNAVAILABLE" not in description


# ─── The bulk bound ─────────────────────────────────────────────────────────────


def test_the_bound_is_advertised_in_both_schemas():
    """A client should be able to constrain the input, not just be told off.

    This is the bound that actually fires over the wire: both SDK majors validate
    arguments against `inputSchema` before the handler runs, so the handler's
    refusal is the fallback for a client that does not. The schema therefore has
    to carry the reasoning too, not just the number.
    """
    for name, parameter in (
        ("generate_proxy_list", "amount"),
        ("build_proxy_connection_string", "count"),
    ):
        spec = _input_schema(_tool(name))["properties"][parameter]
        assert spec["maximum"] == MAX_PROXIES_PER_CALL
        assert spec["minimum"] == 1
        assert str(MAX_PROXIES_PER_CALL) in spec["description"]
        assert "password" in spec["description"], (
            "the description is read before the mistake, so it should say why"
        )


def test_the_bound_is_well_below_what_the_api_allows():
    """GET /public/generate accepts 500; the limit here is a fraction of it."""
    assert MAX_PROXIES_PER_CALL < 500
    assert MAX_PROXIES_PER_CALL >= 10, "must still cover a realistic rotation pool"
