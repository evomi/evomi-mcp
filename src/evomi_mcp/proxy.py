"""Proxy product catalog and connection-string construction.

Pure: it turns credentials the caller already fetched plus a set of targeting
options into connection strings, and performs no I/O.

Sources for the constants and rules below, all under docs.evomi.com:
proxy-instructions/{residential,mobile,datacenter,static-residential}/how-to-connect,
proxy-instructions/proxy-protocols, the Residential "Expert Settings" pages,
and public-api/endpoints/generate.
"""

import re
import secrets as _secrets
from typing import Any, Optional

# Product codes as used by GET /public. The Public API is not internally
# consistent about the datacenter product — /public and /settings call it
# `dcp`, while /generate, /rotate_session and /usage accept only `sdc` — so
# `dcp` is the code used throughout this server and every endpoint that needs
# a different one is given it from the `*_code` fields below. A `None` means
# the endpoint does not serve that product at all.
PRODUCTS: dict[str, dict[str, Any]] = {
    "rp": {
        "name": "Premium Residential",
        "generate_code": "rp",
        "rotate_code": "rp",
        "usage_code": "rp",
        "https_endpoint": ("rp.evomi-proxy.com", 1001),
        "geo": ("country", "region", "city", "zip", "isp", "asn", "continent"),
        "supports_expert_filters": True,
        "supports_adblock": True,
    },
    "rpc": {
        "name": "Core Residential",
        "generate_code": "rpc",
        "rotate_code": "rpc",
        "usage_code": "rpc",
        # No HTTPS-proxy hostname is documented for the core endpoint.
        "https_endpoint": None,
        "geo": ("country", "region", "city", "zip", "asn", "continent"),
        "supports_expert_filters": False,
        "supports_adblock": False,
    },
    "dcp": {
        "name": "Datacenter (Shared)",
        "generate_code": "sdc",
        "rotate_code": "sdc",
        "usage_code": "sdc",
        "https_endpoint": ("dcp.evomi-proxy.com", 2001),
        "geo": ("country", "continent"),
        "supports_expert_filters": False,
        "supports_adblock": True,
    },
    "mp": {
        "name": "Mobile",
        "generate_code": "mp",
        "rotate_code": "mp",
        "usage_code": "mp",
        "https_endpoint": ("mp.evomi-proxy.com", 3001),
        "geo": ("country", "region", "isp", "continent"),
        "supports_expert_filters": False,
        "supports_adblock": True,
    },
    "static_residential": {
        "name": "Static Residential",
        "generate_code": "static_residential",
        "rotate_code": None,
        # Billed per rented IP, so /usage does not accept it.
        "usage_code": None,
        "https_endpoint": None,
        # The rented IP fixes the location, so no targeting parameters apply.
        "geo": (),
        "supports_expert_filters": False,
        "supports_adblock": False,
    },
}

PRODUCT_CODES = tuple(PRODUCTS)
ROTATABLE_PRODUCTS = tuple(code for code, spec in PRODUCTS.items() if spec["rotate_code"])
MEASURABLE_PRODUCTS = tuple(code for code, spec in PRODUCTS.items() if spec["usage_code"])

# GET /public reports Static Residential as packages of IPs with no gateway
# hostname. Gateways are assigned per account, so this documented default is a
# fallback that the caller can override, not a fact about any given account.
STATIC_GATEWAY_HOST = "isp-2.evomi.com"

SESSION_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{6,10}$")

# ISO 3166-1 alpha-2, plus XK for Kosovo, which has no assigned code and which
# the gateway accepts. Availability is per product and per account; this only
# rules out codes no product can have.
COUNTRY_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ
    BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR
    CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
    MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
    PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI
    SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR
    TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW
    """.split()
)

# Codes people reach for that the gateway does not know, with what to send.
COUNTRY_ALIASES = {"UK": "GB", "EL": "GR", "EN": "GB", "USA": "US"}

# Every continent the gateway targets, in its spelling. The catalogue states
# these as display names ("North America"), which the gateway does not take.
CONTINENTS = ("africa", "asia", "europe", "north.america", "oceania", "south.america")

# The gateway's own spelling for a targeting value: lowercase, words joined
# with dots. `list_proxy_targeting_options` reports these as `id`.
WIRE_VALUE_PATTERN = re.compile(r"^[a-z0-9.\-]+$")

# What may appear in a password modifier. The password is interpolated into
# `scheme://user:password@host:port`, so anything outside this set can produce
# a string no HTTP client will parse.
MODIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.,_\-]*$")

FORMAT_TEMPLATES = {
    "url": "{scheme}://{username}:{password}@{host}:{port}",
    "1": "{username}:{password}@{host}:{port}",
    "2": "{host}:{port}:{username}:{password}",
    "3": "{username}:{password}:{host}:{port}",
}


class ProxyOptionError(ValueError):
    """A targeting option is invalid or unsupported for the chosen product."""


def new_session_id(length: int = 8) -> str:
    """Generate a session identifier in the 6-10 alphanumeric range Evomi accepts."""
    return "".join(_secrets.choice(SESSION_ID_ALPHABET) for _ in range(length))


def to_wire_value(value: str, field: str) -> str:
    """
    Render a geo value the way the gateway spells it.

    `list_proxy_targeting_options` reports both spellings for these fields — the
    gateway's under `id`, the human's under `name` — and the two differ by case
    and by spaces standing where dots belong. A display form is accepted here
    and converted; anything left outside the gateway's alphabet is refused,
    because interpolating it into a password produces a connection string that
    no HTTP client can parse.
    """
    wire = re.sub(r"[\s_]+", ".", str(value).strip().lower())

    if not WIRE_VALUE_PATTERN.match(wire):
        raise ProxyOptionError(
            f"'{value}' is not a usable {field}. Call list_proxy_targeting_options "
            f"for the {field} values this product accepts and pass the 'id' field."
        )

    return wire


def normalise_continent(value: str) -> str:
    """
    Check a continent and return it as the gateway wants it.

    A display name is converted; anything that is not one of the six is refused
    here rather than at connect time, where it surfaces as a bare 'failed to
    select from pool'.
    """
    wire = to_wire_value(value, "continent")

    if wire not in CONTINENTS:
        raise ProxyOptionError(
            f"'{value}' is not a continent the gateway targets. Use one of: "
            f"{', '.join(CONTINENTS)}."
        )

    return wire


def _wire_isp(value: str) -> str:
    """
    Check an ISP value and return it as the gateway wants it.

    Unlike the other geo fields, an ISP's display label cannot be converted to
    its wire form: the catalogue truncates the label and drops its spaces, so
    'SAT TELECOMMUNICATIONS LTD' is 'sattelecommunic' and there is no rule that
    recovers the cut. Case is fixed here and anything else is refused.
    """
    wire = str(value).strip().lower()

    if not WIRE_VALUE_PATTERN.match(wire):
        raise ProxyOptionError(
            f"'{value}' is not an ISP identifier the gateway accepts. Call "
            "list_proxy_targeting_options with kind='isps' and pass the 'id' field, "
            "which is not simply the ISP's name in lower case."
        )

    return wire


def normalise_country(code: str) -> str:
    """
    Check one country code and return it as the gateway wants it.

    A code the gateway does not know is refused here rather than at connect
    time, where it surfaces as a bare 'failed to select from pool'.
    """
    cleaned = str(code).strip().upper()

    if cleaned in COUNTRY_CODES:
        return cleaned

    alias = COUNTRY_ALIASES.get(cleaned)
    if alias:
        raise ProxyOptionError(
            f"'{code}' is not an ISO 3166-1 alpha-2 country code. Use '{alias}'."
        )

    raise ProxyOptionError(
        f"'{code}' is not an ISO 3166-1 alpha-2 country code. Use two-letter codes "
        "such as 'US' or 'DE', and call list_proxy_targeting_options to see which "
        "countries this product covers."
    )


def resolve_endpoint(
    product: str,
    protocol: str,
    api_product: dict[str, Any],
) -> tuple[str, int]:
    """
    Pick the host and port to connect to.

    HTTP and SOCKS5 use the endpoint and ports the API reports for this account,
    which is what makes per-account gateways (Static Residential) come out right.
    HTTPS is different: it terminates TLS, so it only works on the hostname the
    certificate covers, and that name is not in the API response — it comes from
    the documented table, and products without one are refused rather than
    guessed at.
    """
    spec = PRODUCTS[product]

    if protocol == "https":
        https_endpoint = spec["https_endpoint"]
        if not https_endpoint:
            raise ProxyOptionError(
                f"{spec['name']} has no HTTPS proxy endpoint. Use protocol 'http' "
                "(it carries HTTPS traffic fine) or 'socks5'."
            )
        return https_endpoint

    host = api_product.get("endpoint")
    port = (api_product.get("ports") or {}).get(protocol)

    if not host and product == "static_residential":
        host = STATIC_GATEWAY_HOST

    if not host or not port:
        raise ProxyOptionError(
            f"The API did not report a {protocol} endpoint for {spec['name']}."
        )

    return host, int(port)


def build_password_modifiers(
    product: str,
    countries: Optional[list[str]] = None,
    region: Optional[str] = None,
    city: Optional[str] = None,
    zip_code: Optional[str] = None,
    continent: Optional[str] = None,
    isp: Optional[str] = None,
    asn: Optional[str] = None,
    session: Optional[str] = None,
    session_id: Optional[str] = None,
    lifetime_minutes: Optional[int] = None,
    mode: Optional[str] = None,
    geosource: Optional[str] = None,
    device: Optional[str] = None,
    max_latency_ms: Optional[int] = None,
    max_fraudscore: Optional[int] = None,
    active_since_minutes: Optional[int] = None,
    adblock: bool = False,
    extended_pool: bool = False,
    http3: bool = False,
    local_dns: bool = False,
    udp: bool = False,
) -> tuple[list[str], Optional[str]]:
    """
    Turn targeting options into the `_key-value` suffixes Evomi appends to the
    proxy password.

    Returns the ordered modifier list and the session id actually used (which is
    generated when a session was asked for without one, so the caller can report
    it back for later rotation).

    Raises ProxyOptionError for combinations the gateway rejects, so the failure
    surfaces here with an explanation rather than as a 407 at connect time.
    """
    if product not in PRODUCTS:
        raise ProxyOptionError(
            f"Unknown product '{product}'. Valid products: {', '.join(PRODUCT_CODES)}."
        )

    spec = PRODUCTS[product]
    modifiers: list[str] = []

    requested_geo = {
        "country": countries,
        "region": region,
        "city": city,
        "zip": zip_code,
        "continent": continent,
        "isp": isp,
        "asn": asn,
    }
    for key, value in requested_geo.items():
        if value and key not in spec["geo"]:
            raise ProxyOptionError(
                f"{spec['name']} does not support {key} targeting. "
                f"Supported: {', '.join(spec['geo']) or 'none'}."
            )

    if countries and len(countries) > 1 and (city or region):
        raise ProxyOptionError(
            "City and region targeting require exactly one country."
        )

    if countries:
        modifiers.append("country-" + ",".join(normalise_country(c) for c in countries))
    if continent:
        modifiers.append(f"continent-{normalise_continent(continent)}")
    if region:
        modifiers.append(f"region-{to_wire_value(region, 'region')}")
    if city:
        modifiers.append(f"city-{to_wire_value(city, 'city')}")
    if zip_code:
        modifiers.append(f"zip-{str(zip_code).strip()}")
    if isp:
        modifiers.append(f"isp-{_wire_isp(isp)}")
    if asn:
        modifiers.append(f"asn-{str(asn).strip().upper()}")

    resolved_session_id: Optional[str] = None
    if session:
        if session not in ("sticky", "hard"):
            raise ProxyOptionError("session must be 'sticky' or 'hard'.")

        if session_id and not SESSION_ID_PATTERN.match(session_id):
            raise ProxyOptionError(
                "session_id must be 6-10 alphanumeric characters."
            )

        resolved_session_id = session_id or new_session_id()
        key = "hardsession" if session == "hard" else "session"
        modifiers.append(f"{key}-{resolved_session_id}")

        if lifetime_minutes is not None:
            if session == "hard":
                raise ProxyOptionError(
                    "lifetime_minutes does not apply to a hard session, which keeps "
                    "its IP for as long as the IP stays online."
                )
            if not 1 <= lifetime_minutes <= 1440:
                raise ProxyOptionError("lifetime_minutes must be between 1 and 1440.")
            modifiers.append(f"lifetime-{lifetime_minutes}")
    elif lifetime_minutes is not None:
        raise ProxyOptionError("lifetime_minutes requires session='sticky'.")

    if mode:
        if mode not in ("standard", "speed", "quality"):
            raise ProxyOptionError("mode must be 'standard', 'speed' or 'quality'.")
        if mode != "standard":
            modifiers.append(f"mode-{mode}")

    if geosource:
        if geosource not in ("ipapi", "maxmind"):
            raise ProxyOptionError("geosource must be 'ipapi' or 'maxmind'.")
        if geosource != "ipapi":
            modifiers.append(f"geosource-{geosource}")

    expert_requested = any(
        value for value in (device, max_latency_ms, max_fraudscore, active_since_minutes)
    ) or extended_pool
    if expert_requested and not spec["supports_expert_filters"]:
        raise ProxyOptionError(
            f"Expert filters (device, latency, fraudscore, activesince, extended pool) "
            f"are only available on {PRODUCTS['rp']['name']}, not {spec['name']}."
        )

    if device:
        if device not in ("windows", "unix", "apple"):
            raise ProxyOptionError("device must be 'windows', 'unix' or 'apple'.")
        modifiers.append(f"device-{device}")
    if max_latency_ms is not None:
        modifiers.append(f"latency-{max_latency_ms}")
    if max_fraudscore is not None:
        modifiers.append(f"fraudscore-{max_fraudscore}")
    if active_since_minutes is not None:
        modifiers.append(f"activesince-{active_since_minutes}")

    if adblock:
        if not spec["supports_adblock"]:
            raise ProxyOptionError(f"Ad blocking is not available on {spec['name']}.")
        modifiers.append("adblock-1")

    if extended_pool:
        modifiers.append("extended-1")
    if http3:
        modifiers.append("http3-1")
    if local_dns:
        modifiers.append("localdns-1")
    if udp:
        modifiers.append("udp-1")

    if modifiers and product == "static_residential":
        raise ProxyOptionError(
            "Static Residential proxies take no targeting parameters — the rented "
            "IP fixes the location and does not rotate."
        )

    for modifier in modifiers:
        if not MODIFIER_PATTERN.match(modifier):
            raise ProxyOptionError(
                f"'{modifier}' cannot go into a proxy password: it would produce a "
                "connection string that no HTTP client can parse. Call "
                "list_proxy_targeting_options and pass the 'id' of the value you want."
            )

    return modifiers, resolved_session_id


def format_proxy(
    template: str,
    scheme: str,
    host: str,
    port: int,
    username: str,
    password: str,
) -> str:
    """Render one connection string in the requested layout."""
    if template not in FORMAT_TEMPLATES:
        raise ProxyOptionError(
            f"format must be one of: {', '.join(FORMAT_TEMPLATES)}."
        )

    return FORMAT_TEMPLATES[template].format(
        scheme=scheme, host=host, port=port, username=username, password=password
    )


def curl_example(scheme: str, host: str, port: int, username: str, password: str) -> str:
    """A copy-pasteable check that the credentials and targeting work."""
    proxy_scheme = "socks5h" if scheme == "socks5" else scheme
    return (
        f"curl -x {proxy_scheme}://{host}:{port} -U '{username}:{password}' "
        "https://ip.evomi.com/s"
    )
