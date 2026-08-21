"""The values this server puts on the wire, for the gateway and for the API.

Two separate contracts, both of which fail silently when they are wrong.

A targeting value goes into the proxy password, and the password goes into
`scheme://user:password@host:port`. A value the gateway does not recognise costs
a connection; a value carrying a space costs the whole string, because no HTTP
client will parse it. So every value is either converted to the gateway's own
spelling or refused here.

A product code goes into a query parameter, and the Public API does not use one
code per product across its endpoints. Sending the wrong one is an HTTP 400 that
reads like an outage.
"""

import pytest

from evomi_mcp.proxy import (
    CONTINENTS,
    COUNTRY_CODES,
    MEASURABLE_PRODUCTS,
    PRODUCTS,
    ProxyOptionError,
    build_password_modifiers,
    format_proxy,
    normalise_country,
    to_wire_value,
)


def modifiers(product="rp", **targeting):
    return build_password_modifiers(product, **targeting)[0]


# ─── Continents ─────────────────────────────────────────────────────────────────

# The six the gateway serves, each with the display form the catalogue shows
# beside it.
CONTINENTS_TABLE = [
    ("africa", "Africa"),
    ("asia", "Asia"),
    ("europe", "Europe"),
    ("north.america", "North America"),
    ("oceania", "Oceania"),
    ("south.america", "South America"),
]


@pytest.mark.parametrize(("wire", "display"), CONTINENTS_TABLE)
def test_a_continent_reaches_the_password_in_the_gateways_spelling(wire, display):
    assert modifiers(continent=wire) == [f"continent-{wire}"]
    assert modifiers(continent=display) == [f"continent-{wire}"]


@pytest.mark.parametrize("given", ["EUROPE", " Europe ", "north america", "North.America"])
def test_a_continent_is_accepted_however_it_is_capitalised_or_spaced(given):
    assert modifiers(continent=given)[0].startswith("continent-")
    assert " " not in modifiers(continent=given)[0]


def test_a_multi_word_continent_never_carries_its_space_through():
    """A space here is what makes a connection string unparseable."""
    assert modifiers(continent="North America") == ["continent-north.america"]


# ─── Regions, cities and ISPs ───────────────────────────────────────────────────


def test_a_region_display_form_is_converted():
    assert modifiers(region="Uttar Pradesh") == ["region-uttar.pradesh"]


def test_a_city_display_form_is_converted():
    assert modifiers(city="New York") == ["city-new.york"]


def test_an_isp_is_lowercased():
    assert modifiers(isp="ComcastCable") == ["isp-comcastcable"]


def test_an_isp_display_label_is_refused_rather_than_guessed_at():
    """The catalogue truncates an ISP label, so no rule recovers its id."""
    with pytest.raises(ProxyOptionError, match="list_proxy_targeting_options"):
        modifiers(isp="SAT TELECOMMUNICATIONS LTD")


@pytest.mark.parametrize(
    "field, value",
    [
        ("continent", "Atlantis!"),
        ("region", "Bavaria/Bayern"),
        ("city", "Berlin@Mitte"),
        ("isp", "comcast cable"),
    ],
)
def test_a_value_outside_the_gateways_alphabet_is_a_clear_error(field, value):
    with pytest.raises(ProxyOptionError, match="list_proxy_targeting_options"):
        modifiers(**{field: value})


def test_to_wire_value_names_the_field_it_refused():
    with pytest.raises(ProxyOptionError, match="continent"):
        to_wire_value("Atlantis!", "continent")


def test_the_six_continents_are_the_ones_the_catalogue_lists():
    assert set(CONTINENTS) == {wire for wire, _ in CONTINENTS_TABLE}


def test_a_continent_that_does_not_exist_lists_the_ones_that_do():
    with pytest.raises(ProxyOptionError, match="north.america"):
        modifiers(continent="Atlantis")


# ─── Countries ──────────────────────────────────────────────────────────────────


def test_a_known_country_is_upper_cased_and_passed_through():
    assert modifiers(countries=["us", " de "]) == ["country-US,DE"]


def test_an_unknown_country_names_the_offending_code():
    with pytest.raises(ProxyOptionError, match="'ZZ'"):
        modifiers(countries=["ZZ"])


def test_one_bad_code_among_good_ones_still_fails():
    with pytest.raises(ProxyOptionError, match="'QQ'"):
        modifiers(countries=["US", "QQ", "DE"])


@pytest.mark.parametrize(("given", "correct"), [("UK", "GB"), ("EL", "GR"), ("USA", "US")])
def test_a_common_wrong_code_is_answered_with_the_right_one(given, correct):
    with pytest.raises(ProxyOptionError, match=correct):
        normalise_country(given)


def test_the_country_set_covers_what_the_gateway_serves():
    """Kosovo has no assigned ISO code and the gateway targets it."""
    assert "XK" in COUNTRY_CODES
    assert {"US", "DE", "GB", "JP", "BR"} <= COUNTRY_CODES
    assert "ZZ" not in COUNTRY_CODES


def test_city_targeting_does_not_require_a_country():
    assert modifiers(city="berlin") == ["city-berlin"]


def test_city_targeting_still_refuses_more_than_one_country():
    with pytest.raises(ProxyOptionError, match="exactly one country"):
        modifiers(countries=["US", "DE"], city="berlin")


# ─── Nothing reaches a connection string that breaks it ─────────────────────────

URL_BREAKING = ' \t"<>@:/?#[]{}|\\^`%'


@pytest.mark.parametrize(
    "targeting",
    [
        {"continent": "North America"},
        {"city": "New York"},
        {"region": "Uttar Pradesh"},
        {"countries": ["US", "DE"]},
        {"isp": "comcastcable"},
        {"asn": "as7922"},
        {"zip_code": "90210"},
        {"session": "sticky", "lifetime_minutes": 30},
        {"mode": "quality", "geosource": "maxmind"},
        {"device": "windows", "max_latency_ms": 500, "max_fraudscore": 20},
    ],
)
def test_no_accepted_targeting_produces_an_unparseable_connection_string(targeting):
    from urllib.parse import urlsplit

    password = "base_pw" + "".join(f"_{m}" for m in modifiers(**targeting))
    url = format_proxy("url", "http", "rp.evomi.com", 1000, "user", password)

    assert not any(ch in password for ch in URL_BREAKING)
    parsed = urlsplit(url)
    assert parsed.hostname == "rp.evomi.com"
    assert parsed.port == 1000
    assert parsed.password == password


@pytest.mark.parametrize(
    "targeting",
    [
        {"city": "Frank furt am Main!"},
        {"continent": "not a continent"},
        {"zip_code": "SW1A 1AA"},
        {"isp": "some isp"},
    ],
)
def test_a_value_that_cannot_be_made_safe_is_refused_not_interpolated(targeting):
    with pytest.raises(ProxyOptionError):
        modifiers(**targeting)


# ─── Product codes, per endpoint ────────────────────────────────────────────────

# What each Public API endpoint accepts for each product, as the API answers
# today. `None` means the endpoint does not serve that product at all: /usage
# and /rotate_session have no Static Residential, and /usage states `dcp` in its
# own parameter enum while its handler rejects it.
WIRE_CODES = {
    "rp": {"generate_code": "rp", "rotate_code": "rp", "usage_code": "rp"},
    "rpc": {"generate_code": "rpc", "rotate_code": "rpc", "usage_code": "rpc"},
    "mp": {"generate_code": "mp", "rotate_code": "mp", "usage_code": "mp"},
    "dcp": {"generate_code": "sdc", "rotate_code": "sdc", "usage_code": "sdc"},
    "static_residential": {
        "generate_code": "static_residential",
        "rotate_code": None,
        "usage_code": None,
    },
}


def test_every_product_is_covered_by_the_wire_code_table():
    assert set(WIRE_CODES) == set(PRODUCTS)


@pytest.mark.parametrize("product", sorted(WIRE_CODES))
@pytest.mark.parametrize("endpoint", ["generate_code", "rotate_code", "usage_code"])
def test_each_endpoint_gets_the_code_it_accepts(product, endpoint):
    assert PRODUCTS[product][endpoint] == WIRE_CODES[product][endpoint]


def test_the_datacenter_product_is_named_differently_on_every_endpoint_that_takes_it():
    """`dcp` is the code /public and /settings use, and the only one they use."""
    assert PRODUCTS["dcp"]["generate_code"] == "sdc"
    assert PRODUCTS["dcp"]["rotate_code"] == "sdc"
    assert PRODUCTS["dcp"]["usage_code"] == "sdc"


def test_measurable_products_are_those_the_usage_endpoint_serves():
    assert set(MEASURABLE_PRODUCTS) == {"rp", "rpc", "mp", "dcp"}
