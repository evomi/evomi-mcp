"""Tool handlers, against a stubbed Public API. No network.

The `stub` fixture and the payload fixtures live in conftest.py, because the
kill-switch audit in test_hidden_passwords.py drives the same handlers.
"""

import json

import pytest

from evomi_mcp.proxy_tools import handle_proxy_tool
from evomi_mcp.public_client import EvomiPublicAPIError
from evomi_mcp.server import ToolError, call_tool


async def call(name, arguments=None):
    return json.loads(await handle_proxy_tool(name, arguments or {}))


# ─── list_proxy_products ────────────────────────────────────────────────────────


async def test_product_list_never_includes_passwords(stub, products_payload):
    stub(products_payload)
    result = await call("list_proxy_products")

    assert "rp_secret_pw" not in json.dumps(result)
    rp = next(p for p in result["products"] if p["product"] == "rp")
    assert rp["username"] == "acct_user"
    assert rp["has_credentials"] is True
    assert rp["balance_gb"] == 5.12


async def test_product_list_reports_zero_balance(stub, products_payload):
    stub(products_payload)
    result = await call("list_proxy_products")

    mobile = next(p for p in result["products"] if p["product"] == "mp")
    assert mobile["has_balance"] is False


async def test_static_residential_is_summarised_by_ip(stub, products_payload):
    stub(products_payload)
    result = await call("list_proxy_products")

    static = next(p for p in result["products"] if p["product"] == "static_residential")
    assert static["rented_ip_count"] == 2
    assert static["ips"][0]["ip"] == "203.0.113.10"
    assert "static_pw_1" not in json.dumps(result)


# ─── get_proxy_credentials ──────────────────────────────────────────────────────


async def test_credentials_returns_the_password(stub, products_payload):
    stub(products_payload)
    result = await call("get_proxy_credentials", {"product": "rpc"})

    assert result["username"] == "acct_user_core"
    assert result["password"] == "rpc_secret_pw"
    assert result["passwords_hidden"] is False


async def test_credentials_masked_when_operator_disables_disclosure(
    stub, products_payload, monkeypatch
):
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "1")
    stub(products_payload)
    result = await call("get_proxy_credentials", {"product": "rp"})

    assert result["password"] == "•" * 8
    assert "rp_secret_pw" not in json.dumps(result)
    assert result["passwords_hidden"] is True


async def test_static_credentials_borrow_the_account_username(stub, products_payload):
    stub(products_payload)
    result = await call("get_proxy_credentials", {"product": "static_residential"})

    assert result["username"] == "acct_user"
    assert result["ips"][1]["password"] == "static_pw_2"


async def test_absent_product_explains_itself(stub):
    stub({"success": True, "products": {}})
    with pytest.raises(Exception, match="no Premium Residential product"):
        await call("get_proxy_credentials", {"product": "rp"})


# ─── build_proxy_connection_string ──────────────────────────────────────────────


async def test_connection_string_uses_api_endpoint_and_targeting(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string",
        {"product": "rp", "countries": ["US"], "session": "sticky", "lifetime_minutes": 10},
    )

    entry = result["proxies"][0]
    session_id = result["session_ids"][0]
    assert entry["proxy"] == (
        f"http://acct_user:rp_secret_pw_country-US_session-{session_id}_lifetime-10"
        "@premium-residential.evomi.com:1000"
    )
    assert entry["session_id"] == session_id
    assert result["curl_example"].startswith("curl -x http://premium-residential.evomi.com:1000")


async def test_each_string_gets_its_own_generated_session(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string", {"product": "rp", "count": 5, "session": "sticky"}
    )

    assert len(result["proxies"]) == 5
    assert len(set(result["session_ids"])) == 5


async def test_supplied_session_id_is_shared_across_strings(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string",
        {"product": "rp", "count": 3, "session": "sticky", "session_id": "ABC123"},
    )

    assert result["session_ids"] == ["ABC123"]
    assert len({entry["proxy"] for entry in result["proxies"]}) == 1
    assert {entry["session_id"] for entry in result["proxies"]} == {"ABC123"}


async def test_socks5_selects_the_socks_port(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string", {"product": "dcp", "protocol": "socks5"}
    )

    assert result["port"] == 2002
    assert result["proxies"][0]["proxy"] == (
        "socks5://acct_user:dcp_secret_pw@dcp.evomi.com:2002"
    )
    # No session was asked for, so there is no session id to report per entry.
    assert "session_id" not in result["proxies"][0]


async def test_https_switches_to_the_certificate_hostname(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string", {"product": "rp", "protocol": "https"}
    )

    assert result["host"] == "rp.evomi-proxy.com"
    assert result["port"] == 1001


async def test_zero_balance_is_called_out(stub, products_payload):
    stub(products_payload)
    result = await call("build_proxy_connection_string", {"product": "mp"})

    assert any("no data balance" in note for note in result["notes"])


async def test_unsupported_targeting_fails_before_a_string_is_built(stub, products_payload):
    stub(products_payload)
    with pytest.raises(Exception, match="Expert filters"):
        await call("build_proxy_connection_string", {"product": "mp", "device": "windows"})


async def test_static_connection_strings_prefix_the_ip(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string",
        {"product": "static_residential", "count": 2, "format": "2"},
    )

    assert result["port"] == 12345
    assert result["host"] == "isp-2.evomi.com"
    assert result["proxies"][0]["proxy"] == (
        "isp-2.evomi.com:12345:acct_user:203.0.113.10_static_pw_1"
    )
    assert len(result["proxies"]) == 2
    assert any("assigned per account" in note for note in result["notes"])


async def test_curl_example_verifies_the_first_proxy(stub, products_payload):
    """The curl command has to correspond to proxies[0].

    With count > 1 and a generated session every string carries its own
    password, so the example is only checkable if it is the one belonging to the
    entry the caller reads first.
    """
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string",
        {"product": "rp", "count": 4, "session": "sticky"},
    )

    first_session = result["proxies"][0]["session_id"]
    last_session = result["proxies"][-1]["session_id"]

    assert first_session != last_session, "the sessions must differ for this to mean anything"
    assert f"session-{first_session}" in result["curl_example"]
    assert f"session-{last_session}" not in result["curl_example"]


async def test_proxies_are_objects_for_every_product(stub, products_payload):
    """One shape, so a consumer never has to branch on the product.

    Objects rather than bare strings for both rotating products and Static
    Residential: an object has somewhere to put the per-entry facts (which
    session id, which rented IP, when it expires) that a bare string cannot
    carry.
    """
    stub(products_payload)

    rotating = await call("build_proxy_connection_string", {"product": "rp"})
    static = await call("build_proxy_connection_string", {"product": "static_residential"})

    for result in (rotating, static):
        assert all(isinstance(entry, dict) for entry in result["proxies"])
        assert all("proxy" in entry for entry in result["proxies"])
        assert result["curl_example"].startswith("curl -x ")


async def test_static_gateway_host_can_be_overridden(stub, products_payload):
    stub(products_payload)
    result = await call(
        "build_proxy_connection_string",
        {"product": "static_residential", "gateway_host": "isp-7.evomi.com"},
    )

    assert result["host"] == "isp-7.evomi.com"


async def test_static_ip_selection_rejects_an_unrented_address(stub, products_payload):
    stub(products_payload)
    with pytest.raises(Exception, match="not one of this account"):
        await call(
            "build_proxy_connection_string",
            {"product": "static_residential", "static_ip": "198.51.100.1"},
        )


# ─── generate_proxy_list ────────────────────────────────────────────────────────


async def test_generate_translates_the_datacenter_product_code(stub, products_payload):
    client = stub(products_payload, generate="http://dcp.evomi.com:2000:u:p\n")
    result = await call(
        "generate_proxy_list", {"product": "dcp", "amount": 2, "countries": ["us", "de"]}
    )

    _, params = client.calls[0]
    assert params["product"] == "sdc"
    assert params["countries"] == "US,DE"
    assert result["count"] == 1


async def test_generate_refuses_more_than_the_bulk_limit(stub, products_payload):
    """Refused, not clamped: 50 lines silently returned for 500 looks like success."""
    client = stub(products_payload, generate="")

    with pytest.raises(Exception, match="capped at 50 per call"):
        await call("generate_proxy_list", {"product": "rp", "amount": 9000})

    assert client.calls == [], "a refused amount must not reach the API"


@pytest.mark.parametrize(
    "tool, parameter",
    [
        ("generate_proxy_list", "amount"),
        ("build_proxy_connection_string", "count"),
    ],
)
async def test_the_bulk_limit_is_inclusive(stub, products_payload, tool, parameter):
    """The boundary itself is allowed; one past it is not."""
    stub(products_payload, generate="\n".join(["u:p@h:1"] * 50))

    at_limit = await call(tool, {"product": "rp", parameter: 50})
    assert len(at_limit["proxies"]) == 50

    with pytest.raises(Exception, match="capped at 50 per call") as excinfo:
        await call(tool, {"product": "rp", parameter: 51})

    # The message has to name the limit, or the caller is left guessing.
    assert "50" in str(excinfo.value) and parameter in str(excinfo.value)


@pytest.mark.parametrize("amount", [0, -3])
async def test_generate_refuses_a_non_positive_amount(stub, products_payload, amount):
    stub(products_payload, generate="")

    with pytest.raises(Exception, match="must be at least 1"):
        await call("generate_proxy_list", {"product": "rp", "amount": amount})


# ─── get_proxy_usage ────────────────────────────────────────────────────────────


USAGE = {
    "success": True,
    "data": {
        "bandwidth": {
            "products": [
                {
                    "name": "Residential",
                    "code": "rpc",
                    "totalBandwidth": 300.0,
                    "stats": {
                        "2026-05-26 00:00": {"default": 100.0, "extra": 0.0, "total": 100.0},
                        "2026-05-27 00:00": {"default": 200.0, "extra": 0.5, "total": 200.5},
                    },
                }
            ]
        }
    },
    "meta": {"period": "3d", "granularity": "day", "total_bandwidth_mb": 300.5},
}


async def test_usage_summarises_total_and_buckets(stub):
    stub({}, usage=USAGE)
    result = await call("get_proxy_usage", {"product": "rpc", "period": "3d"})

    assert result["total_bandwidth_mb"] == 300.5
    assert result["total_bandwidth_gb"] == 0.3
    assert result["bucket_count"] == 2
    assert result["buckets"][-1] == {"time": "2026-05-27 00:00", "total_mb": 200.5}


async def test_usage_caps_the_bucket_series(stub):
    stub({}, usage=USAGE)
    result = await call("get_proxy_usage", {"product": "rpc", "max_buckets": 1})

    assert result["buckets_returned"] == 1
    assert result["bucket_count"] == 2


async def test_usage_can_omit_buckets(stub):
    stub({}, usage=USAGE)
    result = await call("get_proxy_usage", {"product": "rpc", "include_buckets": False})

    assert "buckets" not in result


# ─── list_proxy_targeting_options ───────────────────────────────────────────────


SETTINGS = {
    "success": True,
    "data": {
        "rp": {
            "countries": {"DE": "Germany", "US": "United States", "IN": "India"},
            "cities": {"data": [{"name": "berlin", "region": "BE"}, {"name": "boston", "region": "MA"}]},
            "isp": {"Comcast": {"value": "comcast", "countryCode": "US"}},
        }
    },
}


async def test_targeting_options_filter_and_cap(stub):
    stub({}, settings=SETTINGS)
    result = await call(
        "list_proxy_targeting_options", {"product": "rp", "kind": "cities", "search": "berl"}
    )

    assert result["match_count"] == 1
    assert result["options"][0]["name"] == "berlin"


async def test_targeting_options_respect_the_limit(stub):
    stub({}, settings=SETTINGS)
    result = await call("list_proxy_targeting_options", {"product": "rp", "limit": 2})

    assert result["match_count"] == 3
    assert result["returned"] == 2


async def test_isp_options_expose_the_value_to_send(stub):
    stub({}, settings=SETTINGS)
    result = await call("list_proxy_targeting_options", {"product": "rp", "kind": "isps"})

    assert result["options"][0] == {"name": "Comcast", "value": "comcast", "countryCode": "US"}


async def test_targeting_options_reject_an_unknown_product(stub):
    stub({}, settings=SETTINGS)
    with pytest.raises(Exception, match="no targeting options"):
        await call("list_proxy_targeting_options", {"product": "mp"})


# ─── rotate_proxy_session ───────────────────────────────────────────────────────


async def test_rotate_translates_the_product_code(stub):
    client = stub({})
    result = await call("rotate_proxy_session", {"product": "dcp", "session_id": "AAAAA"})

    assert client.calls[0][1]["product"] == "sdc"
    assert result["rotated"] is True


async def test_rotate_refuses_static_residential(stub):
    stub({})
    with pytest.raises(Exception, match="cannot be rotated"):
        await call(
            "rotate_proxy_session", {"product": "static_residential", "session_id": "AAAAA"}
        )


# ─── get_api_access ─────────────────────────────────────────────────────────────


ACCESS = {
    "scraper": {
        "success": True,
        "has_access": True,
        "credits": 847.5,
        "concurrency": 5,
        "api_key": "abc123def456ghi",
        "endpoint_url": "https://scrape.evomi.com/api/v1",
    },
    "browser": {
        "success": True,
        "has_access": False,
        "credits": 0,
        "concurrency": 0,
        "api_key": None,
        "max_session_length_seconds": 0,
        "endpoint_url": "wss://browser.evomi.com",
    },
}


async def test_api_keys_are_masked_by_default(stub):
    stub({}, **ACCESS)
    result = await call("get_api_access", {})

    assert result["scraper"]["api_key"] == "•" * 8
    assert "abc123def456ghi" not in json.dumps(result)
    assert result["scraper"]["credits"] == 847.5


async def test_api_key_revealed_only_on_request(stub):
    stub({}, **ACCESS)
    result = await call("get_api_access", {"service": "scraper", "include_api_key": True})

    assert result["scraper"]["api_key"] == "abc123def456ghi"
    assert "browser" not in result


async def test_hide_switch_overrides_an_explicit_reveal(stub, monkeypatch):
    monkeypatch.setenv("EVOMI_HIDE_PROXY_PASSWORDS", "true")
    stub({}, **ACCESS)
    result = await call("get_api_access", {"service": "scraper", "include_api_key": True})

    assert result["scraper"]["api_key_revealed"] is False
    assert "abc123def456ghi" not in json.dumps(result)


# ─── order_browser_profile ──────────────────────────────────────────────────────


@pytest.fixture
def spending_enabled(monkeypatch):
    monkeypatch.setenv("EVOMI_ENABLE_SPENDING", "1")


def _profiles(count):
    return {"profiles": [{"id": f"prof_{n}", "os": "windows"} for n in range(count)]}


async def test_order_is_refused_while_spending_is_switched_off(stub):
    client = stub({})

    with pytest.raises(Exception, match="EVOMI_ENABLE_SPENDING"):
        await call("order_browser_profile", {"os": "windows", "browser_version": "120"})

    assert client.calls == [], "a refused order must not reach the API"


async def test_order_passes_the_parameters_through(stub, spending_enabled):
    client = stub({}, profiles=_profiles(0))
    result = await call(
        "order_browser_profile", {"os": " windows ", "browser_version": " 120 "}
    )

    assert client.calls[-1] == (
        "order_browser_profile",
        {"os": "windows", "browser_version": "120"},
    )
    assert result["ordered"] is True
    assert result["profile"] == {"id": "prof_1"}


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"browser_version": "120"}, "os is required"),
        ({"os": "  ", "browser_version": "120"}, "os is required"),
        ({"os": "windows"}, "browser_version is required"),
        ({"os": "windows", "browser_version": ""}, "browser_version is required"),
    ],
)
async def test_order_validates_its_parameters(stub, spending_enabled, arguments, expected):
    client = stub({}, profiles=_profiles(0))

    with pytest.raises(Exception, match=expected):
        await call("order_browser_profile", arguments)

    assert client.calls == [], "validation must fail before anything is spent"


async def test_order_reports_a_free_profile(stub, spending_enabled):
    stub({}, profiles=_profiles(2))
    result = await call(
        "order_browser_profile", {"os": "windows", "browser_version": "120"}
    )

    assert result["profiles_before_this_order"] == 2
    assert result["free_profile_allowance"] == 3
    assert result["expected_cost_gb"] == 0


async def test_order_reports_the_charge_once_the_allowance_is_used(stub, spending_enabled):
    stub({}, profiles=_profiles(3))
    result = await call(
        "order_browser_profile", {"os": "windows", "browser_version": "120"}
    )

    assert result["profiles_before_this_order"] == 3
    assert result["expected_cost_gb"] == 1.5


async def test_order_counts_existing_profiles_before_ordering(stub, spending_enabled):
    client = stub({}, profiles=_profiles(1))
    await call("order_browser_profile", {"os": "windows", "browser_version": "120"})

    assert [name for name, _ in client.calls] == [
        "list_browser_profiles",
        "order_browser_profile",
    ]


async def test_order_survives_an_unreadable_profile_count(stub, spending_enabled):
    stub({}, profiles=EvomiPublicAPIError("Evomi Public API returned HTTP 500."))
    result = await call(
        "order_browser_profile", {"os": "windows", "browser_version": "120"}
    )

    assert result["ordered"] is True
    assert result["profiles_before_this_order"] is None
    assert result["expected_cost_gb"] is None
    assert any("could not be read" in note for note in result["notes"])


async def test_order_failure_explains_itself(stub, spending_enabled):
    stub({}, profiles=_profiles(0), order=EvomiPublicAPIError(
        "Evomi Public API returned HTTP 400: Invalid browser version"
    ))

    with pytest.raises(EvomiPublicAPIError, match="Invalid browser version"):
        await call("order_browser_profile", {"os": "windows", "browser_version": "999"})


async def test_order_error_never_carries_the_api_key(stub, spending_enabled):
    """The scrub happens on the way out of `call_tool`, so it is tested there.

    `call_tool` raises rather than returning the message as content: a failure
    returned as ordinary text is a successful call whose body reads "Error", and
    raising is what makes the client see `isError`.
    """
    stub({}, profiles=_profiles(0), order=EvomiPublicAPIError(
        "Evomi Public API returned HTTP 402: test-public-key-0000 has no balance"
    ))

    with pytest.raises(ToolError) as excinfo:
        await call_tool("order_browser_profile", {"os": "windows", "browser_version": "120"})

    assert "test-public-key-0000" not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)
