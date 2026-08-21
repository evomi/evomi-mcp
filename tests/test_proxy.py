"""Targeting rules and connection-string formatting. Pure, no network."""

import pytest

from evomi_mcp.proxy import (
    PRODUCTS,
    ProxyOptionError,
    build_password_modifiers,
    curl_example,
    format_proxy,
    new_session_id,
    resolve_endpoint,
)


# ─── Modifier construction ──────────────────────────────────────────────────────


def test_country_targeting_uppercases_and_joins():
    modifiers, _ = build_password_modifiers("rp", countries=["us", "de"])
    assert modifiers == ["country-US,DE"]


def test_modifier_order_matches_documented_examples():
    modifiers, _ = build_password_modifiers(
        "rp", countries=["US"], session="sticky", session_id="sgn34f3e", lifetime_minutes=10
    )
    assert modifiers == ["country-US", "session-sgn34f3e", "lifetime-10"]


def test_hard_session_uses_hardsession_key():
    modifiers, session_id = build_password_modifiers(
        "rp", session="hard", session_id="JS9nsq2n"
    )
    assert modifiers == ["hardsession-JS9nsq2n"]
    assert session_id == "JS9nsq2n"


def test_session_id_is_generated_when_omitted():
    modifiers, session_id = build_password_modifiers("rp", session="sticky")
    assert session_id and 6 <= len(session_id) <= 10
    assert modifiers == [f"session-{session_id}"]


def test_expert_filters_render_documented_syntax():
    modifiers, _ = build_password_modifiers(
        "rp",
        zip_code="90210",
        asn="AS7922",
        device="windows",
        max_latency_ms=500,
        max_fraudscore=10,
        active_since_minutes=120,
        http3=True,
        local_dns=True,
        udp=True,
        adblock=True,
    )
    assert modifiers == [
        "zip-90210",
        "asn-AS7922",
        "device-windows",
        "latency-500",
        "fraudscore-10",
        "activesince-120",
        "adblock-1",
        "http3-1",
        "localdns-1",
        "udp-1",
    ]


def test_default_mode_and_geosource_add_nothing():
    modifiers, _ = build_password_modifiers("rp", mode="standard", geosource="ipapi")
    assert modifiers == []


# ─── Rules the gateway would otherwise reject at connect time ───────────────────


def test_city_requires_a_single_country():
    with pytest.raises(ProxyOptionError, match="exactly one country"):
        build_password_modifiers("rp", countries=["US", "DE"], city="berlin")


def test_core_residential_rejects_isp_and_adblock():
    with pytest.raises(ProxyOptionError, match="isp targeting"):
        build_password_modifiers("rpc", isp="comcast")
    with pytest.raises(ProxyOptionError, match="Ad blocking"):
        build_password_modifiers("rpc", adblock=True)


def test_expert_filters_are_premium_residential_only():
    with pytest.raises(ProxyOptionError, match="Expert filters"):
        build_password_modifiers("mp", device="windows")


def test_datacenter_rejects_city_targeting():
    with pytest.raises(ProxyOptionError, match="city targeting"):
        build_password_modifiers("dcp", countries=["US"], city="dallas")


def test_lifetime_requires_a_sticky_session():
    with pytest.raises(ProxyOptionError, match="requires session"):
        build_password_modifiers("rp", lifetime_minutes=10)
    with pytest.raises(ProxyOptionError, match="hard session"):
        build_password_modifiers("rp", session="hard", lifetime_minutes=10)


@pytest.mark.parametrize("minutes", [0, 1441])
def test_lifetime_bounds(minutes):
    with pytest.raises(ProxyOptionError, match="between 1 and 1440"):
        build_password_modifiers("rp", session="sticky", lifetime_minutes=minutes)


@pytest.mark.parametrize("session_id", ["abc", "waytoolongsession"])
def test_session_id_length(session_id):
    with pytest.raises(ProxyOptionError, match="6-10 alphanumeric"):
        build_password_modifiers("rp", session="sticky", session_id=session_id)


def test_static_residential_takes_no_targeting():
    with pytest.raises(ProxyOptionError, match="country targeting"):
        build_password_modifiers("static_residential", countries=["US"])


def test_unknown_product_is_rejected():
    with pytest.raises(ProxyOptionError, match="Unknown product"):
        build_password_modifiers("nope", countries=["US"])


# ─── Endpoint resolution ────────────────────────────────────────────────────────


def test_http_and_socks5_use_the_endpoint_the_api_reported():
    entry = {"endpoint": "premium-residential.evomi.com", "ports": {"http": 1000, "socks5": 1002}}
    assert resolve_endpoint("rp", "http", entry) == ("premium-residential.evomi.com", 1000)
    assert resolve_endpoint("rp", "socks5", entry) == ("premium-residential.evomi.com", 1002)


def test_https_uses_the_certificate_hostname_not_the_api_one():
    entry = {"endpoint": "premium-residential.evomi.com", "ports": {"http": 1000}}
    assert resolve_endpoint("rp", "https", entry) == ("rp.evomi-proxy.com", 1001)


@pytest.mark.parametrize("product", ["rpc", "static_residential"])
def test_https_refused_where_no_endpoint_is_documented(product):
    entry = {"endpoint": "core-residential.evomi.com", "ports": {"http": 1000}}
    with pytest.raises(ProxyOptionError, match="no HTTPS proxy endpoint"):
        resolve_endpoint(product, "https", entry)


def test_missing_port_is_reported_rather_than_guessed():
    with pytest.raises(ProxyOptionError, match="did not report"):
        resolve_endpoint("rp", "socks5", {"endpoint": "rp.evomi.com", "ports": {"http": 1000}})


# ─── Formatting ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "template,expected",
    [
        ("url", "http://user:pass@host.example:1000"),
        ("1", "user:pass@host.example:1000"),
        ("2", "host.example:1000:user:pass"),
        ("3", "user:pass:host.example:1000"),
    ],
)
def test_format_templates(template, expected):
    assert format_proxy(template, "http", "host.example", 1000, "user", "pass") == expected


def test_unknown_format_is_rejected():
    with pytest.raises(ProxyOptionError, match="format must be"):
        format_proxy("9", "http", "host.example", 1000, "user", "pass")


def test_curl_example_uses_socks5h_for_dns_at_the_proxy():
    command = curl_example("socks5", "rp.evomi.com", 1002, "user", "pass")
    assert command.startswith("curl -x socks5h://rp.evomi.com:1002")


def test_generated_session_ids_are_unique_enough():
    assert len({new_session_id() for _ in range(200)}) > 190


def test_every_product_declares_a_generate_code():
    assert all(spec["generate_code"] for spec in PRODUCTS.values())
