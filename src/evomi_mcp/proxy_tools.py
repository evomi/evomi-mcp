"""MCP tools for the Evomi Public API: proxy credentials, usage and targeting.

Tool descriptions are written for a model deciding whether to call them, so each
one states what it returns — in particular whether it returns a credential — and
whether it changes anything.

Three tools return a live proxy password. Each of their descriptions carries the
echo-suppression wording below, and `EVOMI_HIDE_PROXY_PASSWORDS` is enforced at
every path that could emit a credential.
"""

import json
import os
from typing import Any, Optional

from mcp.types import Tool

from .annotations import (
    BILLABLE_CREATE,
    DISRUPTIVE,
    FRESH_LOOKUP,
    LOOKUP,
)
from .proxy import (
    PRODUCT_CODES,
    PRODUCTS,
    ROTATABLE_PRODUCTS,
    ProxyOptionError,
    build_password_modifiers,
    curl_example,
    format_proxy,
    resolve_endpoint,
)
from .public_client import EvomiPublicAPIError, EvomiPublicClient
from .security import mask_secret

_public_client: EvomiPublicClient | None = None

USAGE_PERIODS = ("24h", "3d", "7d")
PROTOCOLS = ("http", "https", "socks5")

# POST /public/order-profile is free for the first three profiles on an account
# and deducts this much data balance for every profile after them.
FREE_BROWSER_PROFILES = 3
BROWSER_PROFILE_COST_GB = 1.5

HIDE_ENV_VAR = "EVOMI_HIDE_PROXY_PASSWORDS"

# How many credential-bearing strings one call may return. Well under the 500
# GET /public/generate allows: every entry is a live credential that lands in a
# conversation log, and a genuinely bulk list belongs in the dashboard.
MAX_PROXIES_PER_CALL = 50


def get_public_client() -> EvomiPublicClient:
    """Get or create the Evomi Public API client."""
    global _public_client
    if _public_client is None:
        _public_client = EvomiPublicClient()
    return _public_client


def passwords_hidden() -> bool:
    """Whether the operator has switched off credential disclosure."""
    return os.getenv(HIDE_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def spending_enabled() -> bool:
    """
    Whether the operator has opted in to tools that spend the account's balance.

    Off unless EVOMI_ENABLE_SPENDING says otherwise; the documented value is 1.
    """
    return os.getenv("EVOMI_ENABLE_SPENDING", "").strip().lower() in ("1", "true", "yes")


def echo_suppression(secret: str) -> str:
    """
    The sentence appended to every tool whose output carries a live secret.

    The MCP spec has no annotation for "this result is sensitive", so the
    description is the only channel that reaches the model. This is a request to
    a model, not an enforcement mechanism; `EVOMI_HIDE_PROXY_PASSWORDS` is the
    enforcement mechanism.

    Args:
        secret: what the value is, phrased to slot into "the {secret} ... is a
            live credential" — kept short so it does not crowd out the
            functional half of the description.
    """
    return (
        f" SECRET HANDLING: the {secret} is a live credential. Use it to carry "
        "out the task — write it into the config, command or code the user "
        "asked for — and do not repeat the value back in your reply. Show it "
        "only if the user explicitly asks to see it."
    )


# Appended instead, to the same tools, when the operator has hidden passwords,
# so a model can pick a credential-free tool without spending a call.
def _unavailable_notice(alternatives: str) -> str:
    return (
        f" CURRENTLY UNAVAILABLE: this server runs with {HIDE_ENV_VAR} set, so "
        "this tool refuses instead of returning a password. "
        f"{alternatives}"
    )


_CREDENTIAL_FREE_ALTERNATIVES = (
    "list_proxy_products gives the gateway hostname, ports and balance, and "
    "list_proxy_targeting_options gives the targeting values, neither with a "
    "credential in it."
)


def _refuse_hidden(tool: str, reason: str) -> ProxyOptionError:
    """
    The refusal raised when a password-bearing tool is called under the flag.

    Names the variable, so the reply says the server is configured this way
    rather than that the tool is broken, and points at what still works.
    """
    return ProxyOptionError(
        f"{tool} is unavailable because this server runs with {HIDE_ENV_VAR} "
        f"set. {reason} Ask the operator to unset {HIDE_ENV_VAR} if you need a "
        f"working credential. In the meantime: {_CREDENTIAL_FREE_ALTERNATIVES}"
    )


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _round_mb(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _static_packages(api_product: dict[str, Any]) -> list[dict[str, Any]]:
    packages = api_product.get("packages") or []
    return [p for p in packages if isinstance(p, dict)]


def _static_ips(api_product: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the rented IPs out of the Static Residential package list."""
    ips: list[dict[str, Any]] = []
    for package in _static_packages(api_product):
        for ip in package.get("ips") or []:
            if not isinstance(ip, dict):
                continue
            ips.append(
                {
                    "ip": (ip.get("ipInfo") or {}).get("ip"),
                    "password": ip.get("password"),
                    "expiry_date": package.get("expiryDate"),
                }
            )
    return ips


# ─── Tool Definitions ───────────────────────────────────────────────────────────


def proxy_tool_definitions() -> list[Tool]:
    """
    MCP tool definitions for the Evomi Public (proxy) API.

    Runs on every `tools/list`, so both environment gates are read here and the
    descriptions reflect how the server is configured: `EVOMI_ENABLE_SPENDING`
    decides whether a tool is offered at all (see spending_tool_definitions),
    and `EVOMI_HIDE_PROXY_PASSWORDS` decides whether the credential tools
    describe themselves as working or as refusing.
    """
    hidden = passwords_hidden()
    product_enum = list(PRODUCT_CODES)
    product_help = ", ".join(f"{code} = {spec['name']}" for code, spec in PRODUCTS.items())

    targeting_properties = {
        "countries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Two-letter ISO country codes, e.g. ['US','DE']. Several codes "
            "means one is picked at random per connection. City and region targeting "
            "require exactly one country.",
        },
        "region": {"type": "string", "description": "Region/state name, exactly as returned by list_proxy_targeting_options."},
        "city": {"type": "string", "description": "City name, exactly as returned by list_proxy_targeting_options."},
        "zip_code": {"type": "string", "description": "Postal code, e.g. '90210'. Premium Residential and Core Residential only."},
        "continent": {"type": "string", "description": "Continent name, e.g. 'europe'."},
        "isp": {"type": "string", "description": "ISP name, e.g. 'comcast'. Not available on Core Residential."},
        "asn": {"type": "string", "description": "Autonomous system number, e.g. 'AS7922'."},
        "session": {
            "type": "string",
            "enum": ["sticky", "hard"],
            "description": "Keep the same exit IP across requests. 'sticky' favours success "
            "rate and may swap IP; 'hard' keeps one IP for as long as it stays online. "
            "Omit for a new IP on every request.",
        },
        "session_id": {
            "type": "string",
            "description": "Reuse an existing session identifier (6-10 alphanumeric characters). "
            "One is generated and reported back if omitted.",
        },
        "lifetime_minutes": {
            "type": "integer",
            "description": "How long a sticky session holds its IP, 1-1440 (default 30). Not valid with a hard session.",
        },
        "mode": {
            "type": "string",
            "enum": ["standard", "speed", "quality"],
            "description": "Residential pool selection: standard (largest pool), speed (lowest latency), quality (highest success rate, smallest pool).",
        },
        "geosource": {
            "type": "string",
            "enum": ["ipapi", "maxmind"],
            "description": "Which geolocation database the geo filters are matched against. Default ipapi.",
        },
        "device": {"type": "string", "enum": ["windows", "unix", "apple"], "description": "Exit device OS. Premium Residential only."},
        "max_latency_ms": {"type": "integer", "description": "Only use IPs below this latency in ms. Premium Residential only."},
        "max_fraudscore": {"type": "integer", "description": "Maximum fraud score, 0-100. Premium Residential only."},
        "active_since_minutes": {"type": "integer", "description": "Only use IPs online at least this many minutes. Premium Residential only."},
        "adblock": {"type": "boolean", "default": False, "description": "Block ads at the proxy. Not available on Core Residential."},
        "extended_pool": {"type": "boolean", "default": False, "description": "Use the 4-6x larger extended pool. Disables expert filters. Premium Residential only."},
        "http3": {"type": "boolean", "default": False, "description": "Allow HTTP/3 to the target."},
        "local_dns": {"type": "boolean", "default": False, "description": "Resolve DNS at the exit node rather than at the gateway."},
        "udp": {"type": "boolean", "default": False, "description": "Enable UDP support."},
    }

    tools = [
        # ─── Proxy Products & Credentials ───────────────────────────────────────
        Tool(
            name="list_proxy_products",
            description=(
                "List the Evomi proxy products on this account with their gateway "
                "hostname, HTTP/SOCKS5 ports, remaining data balance, and proxy "
                f"username. Products: {product_help}. Returns NO proxy passwords — "
                "use get_proxy_credentials or build_proxy_connection_string for those. "
                "Call this first to see what the account actually has balance on."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=LOOKUP,
        ),
        Tool(
            name="get_proxy_credentials",
            description=(
                "Get the proxy username and PLAINTEXT PASSWORD for one Evomi proxy "
                "product, along with its gateway hostname, ports and remaining balance. "
                "Returns a live credential that grants paid proxy access — prefer "
                "build_proxy_connection_string when the user just wants something to "
                "paste into a client. Read-only: it does not rotate or change the "
                "password."
            )
            + (
                # Masked rather than refused: the username, endpoint, ports and
                # balance are useful on their own.
                f" With {HIDE_ENV_VAR} set, as it is on this server, the password "
                "comes back masked and the rest of the response is unchanged."
                if hidden
                else echo_suppression("password")
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {"type": "string", "enum": product_enum, "description": product_help},
                },
                "required": ["product"],
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="build_proxy_connection_string",
            description=(
                "Build ready-to-use Evomi proxy connection strings with geo targeting, "
                "sticky sessions and expert filters encoded into the password, plus a "
                "curl command to verify them. This is the tool to use when someone asks "
                "for 'a US residential proxy', 'proxies for Berlin', 'a sticky session', "
                "or how to configure a proxy in code. Fetches the account's own "
                "credentials, so the output CONTAINS THE PROXY PASSWORD. Validates the "
                "targeting against the chosen product and explains any unsupported "
                "combination instead of returning a string that would fail at connect "
                "time. Read-only."
            )
            + (
                _unavailable_notice(_CREDENTIAL_FREE_ALTERNATIVES)
                if hidden
                else echo_suppression("password inside each connection string")
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {"type": "string", "enum": product_enum, "description": product_help},
                    "protocol": {
                        "type": "string",
                        "enum": list(PROTOCOLS),
                        "default": "http",
                        "description": "http carries HTTPS traffic fine and is fastest; https encrypts "
                        "the hop to the proxy (Premium Residential, Mobile, Datacenter only); socks5 "
                        "carries any TCP traffic.",
                    },
                    "count": {
                        "type": "integer",
                        "default": 1,
                        "minimum": 1,
                        "maximum": MAX_PROXIES_PER_CALL,
                        # Stated in the schema as well as in the handler's
                        # refusal, because the schema is what rejects the call.
                        "description": f"How many connection strings to build "
                        f"(1-{MAX_PROXIES_PER_CALL}). Each gets its own session id "
                        f"when a session is requested. Capped at "
                        f"{MAX_PROXIES_PER_CALL} because every entry embeds a live "
                        "proxy password; for a larger pool use the Evomi dashboard's "
                        "generator rather than a conversation.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["url", "1", "2", "3"],
                        "default": "url",
                        "description": "url = scheme://user:pass@host:port (use this for code), "
                        "1 = user:pass@host:port, 2 = host:port:user:pass, 3 = user:pass:host:port.",
                    },
                    "static_ip": {"type": "string", "description": "Static Residential only: which rented IP to exit from. All rented IPs are returned if omitted."},
                    "gateway_host": {
                        "type": "string",
                        "description": "Static Residential only: override the gateway hostname. "
                        "Gateways are assigned per account and the API does not report which "
                        "one, so pass the host shown next to the IPs in the dashboard if the "
                        "default does not work.",
                    },
                    **targeting_properties,
                },
                "required": ["product"],
            },
            # Read-only, but not idempotent: a session that was asked for
            # without an id gets a freshly generated one, so two identical calls
            # return two different sets of strings.
            annotations=FRESH_LOOKUP,
        ),
        Tool(
            name="generate_proxy_list",
            description=(
                "Generate a bulk list of Evomi proxy strings server-side via the Public "
                f"API (up to {MAX_PROXIES_PER_CALL} per call), for pasting into tools "
                "that take a proxy list file. Output CONTAINS PROXY PASSWORDS. Supports "
                "only country/city/region/ISP targeting, sessions and ad blocking — for "
                "zip, ASN, pool mode or expert filters use "
                "build_proxy_connection_string instead. Fails if the product has no "
                "data balance. Read-only."
            )
            + (
                _unavailable_notice(_CREDENTIAL_FREE_ALTERNATIVES)
                if hidden
                else echo_suppression("password inside each returned proxy line")
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {"type": "string", "enum": product_enum, "description": product_help},
                    "amount": {
                        "type": "integer",
                        "default": 1,
                        "minimum": 1,
                        "maximum": MAX_PROXIES_PER_CALL,
                        "description": f"How many proxies to generate "
                        f"(1-{MAX_PROXIES_PER_CALL}). Capped well below the 500 the "
                        "Public API allows because every line embeds a live proxy "
                        "password; for a larger list use the Evomi dashboard's "
                        "generator rather than a conversation.",
                    },
                    "countries": {"type": "array", "items": {"type": "string"}, "description": "Two-letter ISO country codes."},
                    "city": {"type": "string"},
                    "region": {"type": "string"},
                    "isp": {"type": "string"},
                    "session": {"type": "string", "enum": ["sticky", "hard"]},
                    "lifetime_minutes": {"type": "integer", "description": "Sticky session duration, 1-1440."},
                    "protocol": {"type": "string", "enum": ["http", "socks5"], "default": "http"},
                    "format": {"type": "string", "enum": ["1", "2", "3"], "default": "1", "description": "1 = user:pass@host:port, 2 = host:port:user:pass, 3 = user:pass:host:port."},
                    "prepend_protocol": {"type": "boolean", "default": True, "description": "Prefix each line with http:// or socks5://."},
                    "adblock": {"type": "boolean", "default": False},
                },
                "required": ["product"],
            },
            annotations=FRESH_LOOKUP,
        ),
        # ─── Usage & Targeting ──────────────────────────────────────────────────
        Tool(
            name="get_proxy_usage",
            description=(
                "Get bandwidth used for one Evomi proxy product over the last 24 hours, "
                "3 days or 7 days, as a period total plus per-bucket figures — the same "
                "numbers as the dashboard usage chart. Use for 'how much traffic have I "
                "used', spend checks and usage trends. For how much balance is LEFT, use "
                "list_proxy_products instead. Returns no credentials. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {"type": "string", "enum": product_enum, "description": product_help},
                    "period": {"type": "string", "enum": list(USAGE_PERIODS), "default": "3d", "description": "24h gives hourly buckets; 3d and 7d give daily buckets."},
                    "include_buckets": {"type": "boolean", "default": True, "description": "Include the per-bucket series as well as the total."},
                    "max_buckets": {"type": "integer", "default": 24, "description": "Cap on how many of the most recent buckets to return."},
                },
                "required": ["product"],
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="list_proxy_targeting_options",
            description=(
                "Look up which countries, regions, cities, ISPs or continents can be "
                "targeted on a given Evomi proxy product, with an optional search "
                "filter. Call this before build_proxy_connection_string whenever a city, "
                "region or ISP is involved: those values must match exactly, and this "
                "returns the accepted spellings. The full catalogue is large, so results "
                "are filtered and capped. Returns no credentials. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {"type": "string", "enum": ["rp", "rpc", "mp", "dcp"], "description": product_help},
                    "kind": {
                        "type": "string",
                        "enum": ["countries", "regions", "cities", "isps", "continents"],
                        "default": "countries",
                    },
                    "search": {"type": "string", "description": "Case-insensitive substring filter, e.g. 'berl' or 'comcast'."},
                    "limit": {"type": "integer", "default": 50, "description": "Maximum entries to return (1-500)."},
                },
                "required": ["product"],
            },
            annotations=LOOKUP,
        ),
        # ─── Sessions ───────────────────────────────────────────────────────────
        Tool(
            name="rotate_proxy_session",
            # The MUTATING prefix is in the description as well as in the
            # annotations: the description reaches every model, the annotations
            # only clients that read them.
            description=(
                "MUTATING: force an existing Evomi sticky session onto a new exit IP. "
                "The current connection is dropped immediately and the old IP is "
                "released; the session id and credentials stay the same. Use when a "
                "session's IP has been blocked. Only call when the user asks to rotate "
                "or change IP — it disrupts anything currently using that session. "
                "Returns no credentials."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "product": {"type": "string", "enum": list(ROTATABLE_PRODUCTS), "description": product_help},
                    "session_id": {"type": "string", "description": "The session identifier used in the proxy password."},
                },
                "required": ["product", "session_id"],
            },
            annotations=DISRUPTIVE,
        ),
        # ─── Other Product Access ───────────────────────────────────────────────
        Tool(
            name="get_api_access",
            description=(
                "Check this account's access to Evomi's Scraper API and Scraping Browser: "
                "whether it is enabled, remaining credits, concurrency limit, max session "
                "length, and the endpoint URL to connect to. The per-service API keys are "
                "MASKED unless include_api_key is set to true. Use this to find out why a "
                "scrape failed for lack of credits, or to retrieve the scraper key needed "
                "to configure EVOMI_SCRAPER_API_KEY. Read-only."
            )
            + (
                f" With {HIDE_ENV_VAR} set, as it is on this server, the keys stay "
                "masked even when include_api_key is true; everything else in the "
                "response is unchanged."
                if hidden
                else echo_suppression("API key returned when include_api_key is true")
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["scraper", "browser", "all"], "default": "all"},
                    "include_api_key": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return the service API key in plaintext. It will be visible in this conversation.",
                    },
                },
            },
            annotations=LOOKUP,
        ),
        Tool(
            name="list_browser_profiles",
            description=(
                "List the saved browser fingerprint profiles on this account (id, OS, "
                "browser, version, creation date). Use to find a profile id before "
                "launching a browser session. Does not return the fingerprint payload "
                "itself and returns no credentials. Read-only."
            ),
            inputSchema={"type": "object", "properties": {}},
            annotations=LOOKUP,
        ),
    ]

    # The gate is here, at registration, rather than inside the handler: a tool
    # the operator has not opted into is never offered to the model at all, so
    # the model cannot see an irreversible spend and decide to try it.
    if spending_enabled():
        tools.extend(spending_tool_definitions())

    return tools


def spending_tool_definitions() -> list[Tool]:
    """
    Tools that spend the account's balance. Registered only when opted in.

    Kept out of proxy_tool_definitions() so the gate is a single call site
    rather than a condition buried in a long list literal.
    """
    return [
        Tool(
            name="order_browser_profile",
            description=(
                "MUTATING AND SPENDS THE ACCOUNT'S BALANCE: orders a new browser "
                "fingerprint profile. The first "
                f"{FREE_BROWSER_PROFILES} profiles on an account are free; every "
                f"profile after that DEDUCTS {BROWSER_PROFILE_COST_GB} GB of data "
                "balance, and nothing here can undo the charge. Only call it when "
                "the user has explicitly asked for a profile to be created — never "
                "to find out what a profile looks like (use list_browser_profiles), "
                "and never as a retry after an unclear result. Call "
                "list_browser_profiles first: how many profiles already exist is "
                "what decides whether this one is free, and this tool reports that "
                "count alongside the result. Returns no credentials."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "os": {
                        "type": "string",
                        "description": "Operating system the fingerprint should present, e.g. "
                        "'windows'. Must be a value the profile service accepts — "
                        "list_browser_profiles shows the spellings existing profiles use.",
                    },
                    "browser_version": {
                        "type": "string",
                        "description": "Browser version the fingerprint should present, e.g. "
                        "'120'. Must be a value the profile service accepts — "
                        "list_browser_profiles shows the versions existing profiles use.",
                    },
                },
                "required": ["os", "browser_version"],
            },
            # destructiveHint is false even though this one costs money: the
            # spec's "destructive" means an update that replaces or removes
            # something, and this only adds a profile. No hint in the spec means
            # "spends the customer's balance", which is why that warning lives
            # in the description and in the registration gate instead.
            annotations=BILLABLE_CREATE,
        ),
    ]


# Dispatch deliberately covers the gated tools too. Registration decides what a
# client is offered; a name that was never offered should still reach its own
# handler — which refuses it — rather than fall through to the scraper dispatch
# and answer "unknown tool" to something that is merely switched off.
PROXY_TOOL_NAMES = frozenset(
    tool.name for tool in (*proxy_tool_definitions(), *spending_tool_definitions())
)


# ─── Tool Handlers ──────────────────────────────────────────────────────────────


async def handle_proxy_tool(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a Public API tool call. Returns the text body for the MCP response."""
    client = get_public_client()

    if name == "list_proxy_products":
        return await _list_proxy_products(client)

    if name == "get_proxy_credentials":
        return await _get_proxy_credentials(client, arguments["product"])

    if name == "build_proxy_connection_string":
        return await _build_connection_strings(client, arguments)

    if name == "generate_proxy_list":
        return await _generate_proxy_list(client, arguments)

    if name == "get_proxy_usage":
        return await _get_proxy_usage(client, arguments)

    if name == "list_proxy_targeting_options":
        return await _list_targeting_options(client, arguments)

    if name == "rotate_proxy_session":
        return await _rotate_session(client, arguments)

    if name == "get_api_access":
        return await _get_api_access(client, arguments)

    if name == "list_browser_profiles":
        return _json({"profiles": _profile_list(await client.list_browser_profiles())})

    if name == "order_browser_profile":
        # Second line of defence behind the registration gate: an unregistered
        # tool must not become callable just because a client guessed the name.
        if not spending_enabled():
            raise ProxyOptionError(
                "order_browser_profile is switched off on this server. It spends the "
                "account's data balance, so it is only available when the operator "
                "sets EVOMI_ENABLE_SPENDING=1."
            )
        return await _order_browser_profile(client, arguments)

    raise ValueError(f"Unknown proxy tool: {name}")


async def _fetch_products(client: EvomiPublicClient, product: str) -> tuple[dict, dict]:
    """
    Fetch GET /public, returning the whole product map and the requested entry.

    The whole map comes back because Static Residential is reported without a
    username and has to borrow the account's proxy username from a rotating
    product.
    """
    if product not in PRODUCTS:
        raise ProxyOptionError(
            f"Unknown product '{product}'. Valid products: {', '.join(PRODUCT_CODES)}."
        )

    payload = await client.get_proxy_products()
    products = payload.get("products") or {}
    entry = products.get(product)

    if not isinstance(entry, dict):
        raise ProxyOptionError(
            f"The Evomi Public API returned no {PRODUCTS[product]['name']} product for "
            "this account."
        )

    return products, entry


def _account_username(products: dict[str, Any]) -> str | None:
    """The proxy username shared by every product except Core Residential."""
    for code in ("rp", "mp", "dcp"):
        entry = products.get(code)
        if isinstance(entry, dict) and entry.get("username"):
            return entry["username"]
    return None


async def _list_proxy_products(client: EvomiPublicClient) -> str:
    payload = await client.get_proxy_products()
    products = payload.get("products") or {}

    summary = []
    for code, spec in PRODUCTS.items():
        entry = products.get(code)
        if not isinstance(entry, dict):
            continue

        item: dict[str, Any] = {
            "product": code,
            "name": spec["name"],
            "endpoint": entry.get("endpoint"),
            "ports": entry.get("ports"),
        }

        if code == "static_residential":
            ips = _static_ips(entry)
            item["rented_ip_count"] = len(ips)
            item["ips"] = [
                {"ip": ip["ip"], "expiry_date": ip["expiry_date"]} for ip in ips[:25]
            ]
            item["has_credentials"] = any(ip["password"] for ip in ips)
        else:
            balance_mb = _round_mb(entry.get("balance_mb"))
            item["username"] = entry.get("username")
            item["balance_mb"] = balance_mb
            item["balance_gb"] = round(balance_mb / 1000, 3) if balance_mb is not None else None
            item["has_balance"] = bool(balance_mb)
            item["has_credentials"] = bool(entry.get("password"))

        summary.append(item)

    return _json(
        {
            "products": summary,
            "note": "Passwords are not included here. Use get_proxy_credentials or "
            "build_proxy_connection_string.",
        }
    )


async def _get_proxy_credentials(client: EvomiPublicClient, product: str) -> str:
    products, entry = await _fetch_products(client, product)
    hidden = passwords_hidden()

    if product == "static_residential":
        ips = _static_ips(entry)
        return _json(
            {
                "product": product,
                "name": PRODUCTS[product]["name"],
                "username": _account_username(products),
                "ports": entry.get("ports"),
                "credential_format": "Password is the exit IP, an underscore, then the "
                "IP's password — e.g. '203.0.113.10_secret'.",
                "ips": [
                    {
                        "ip": ip["ip"],
                        "password": mask_secret(ip["password"]) if hidden else ip["password"],
                        "expiry_date": ip["expiry_date"],
                    }
                    for ip in ips
                ],
                "passwords_hidden": hidden,
            }
        )

    password = entry.get("password")
    balance_mb = _round_mb(entry.get("balance_mb"))

    return _json(
        {
            "product": product,
            "name": PRODUCTS[product]["name"],
            "username": entry.get("username"),
            "password": mask_secret(password) if hidden else password,
            "passwords_hidden": hidden,
            "endpoint": entry.get("endpoint"),
            "ports": entry.get("ports"),
            "balance_mb": balance_mb,
            "balance_gb": round(balance_mb / 1000, 3) if balance_mb is not None else None,
        }
    )


def _bounded_count(value: Any, parameter: str) -> int:
    """
    Read a proxy count, refusing anything past MAX_PROXIES_PER_CALL.

    Refusing rather than clamping: silently returning 50 strings to someone who
    asked for 500 looks like success and leaves them wondering later why their
    rotation pool is short. The message carries the limit and where to go for
    more, so the caller can decide instead of guessing.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ProxyOptionError(f"{parameter} must be a whole number.") from None

    if count < 1:
        raise ProxyOptionError(f"{parameter} must be at least 1.")

    if count > MAX_PROXIES_PER_CALL:
        raise ProxyOptionError(
            f"{parameter} is capped at {MAX_PROXIES_PER_CALL} per call, and "
            f"{count} were requested. Each entry embeds a live proxy password, "
            "so this server does not return them by the hundred into a "
            f"conversation. Ask again for {MAX_PROXIES_PER_CALL} or fewer, or "
            "generate a larger list from the Evomi dashboard or the Public API "
            "directly, where it does not pass through an AI client."
        )

    return count


async def _build_connection_strings(
    client: EvomiPublicClient, arguments: dict[str, Any]
) -> str:
    if passwords_hidden():
        raise _refuse_hidden(
            "build_proxy_connection_string",
            "Every string it returns embeds the account's proxy password, and a "
            "masked one would not connect — it would only look like it should.",
        )

    product = arguments["product"]
    protocol = arguments.get("protocol", "http")
    template = arguments.get("format", "url")
    count = _bounded_count(arguments.get("count", 1), "count")

    if protocol not in PROTOCOLS:
        raise ProxyOptionError(f"protocol must be one of: {', '.join(PROTOCOLS)}.")

    products, entry = await _fetch_products(client, product)
    host, port = resolve_endpoint(product, protocol, entry)
    host = arguments.get("gateway_host") or host
    scheme = "socks5" if protocol == "socks5" else "http"

    if product == "static_residential":
        return _build_static_strings(
            products, entry, arguments, host, port, scheme, template, count
        )

    username = entry.get("username")
    base_password = entry.get("password")

    if not username or not base_password:
        raise ProxyOptionError(
            f"No {PRODUCTS[product]['name']} credentials on this account. "
            "Call list_proxy_products to see which products are available."
        )

    if not entry.get("balance_mb"):
        note_balance = (
            f"{PRODUCTS[product]['name']} has no data balance left; these strings will "
            "authenticate but requests will fail until the account is topped up."
        )
    else:
        note_balance = None

    targeting = {
        key: arguments.get(key)
        for key in (
            "countries", "region", "city", "zip_code", "continent", "isp", "asn",
            "session", "session_id", "lifetime_minutes", "mode", "geosource",
            "device", "max_latency_ms", "max_fraudscore", "active_since_minutes",
        )
    }
    flags = {
        key: bool(arguments.get(key, False))
        for key in ("adblock", "extended_pool", "http3", "local_dns", "udp")
    }

    # A caller-supplied session id is meant to be reused across every string; a
    # generated one identifies a single session, so each string gets its own.
    # Each entry is an object, matching the Static Residential shape below, so
    # it can carry the session id belonging to it.
    proxies: list[dict[str, Any]] = []
    session_ids: list[str] = []
    first_password: str | None = None
    for _ in range(count):
        modifiers, session_id = build_password_modifiers(product, **targeting, **flags)
        password = base_password + "".join(f"_{m}" for m in modifiers)
        if first_password is None:
            first_password = password

        entry: dict[str, Any] = {
            "proxy": format_proxy(template, scheme, host, port, username, password)
        }
        if session_id:
            entry["session_id"] = session_id
        proxies.append(entry)

        if session_id and session_id not in session_ids:
            session_ids.append(session_id)

    notes = [n for n in (note_balance,) if n]
    if protocol == "https":
        notes.append(
            "The HTTPS proxy port only accepts the evomi-proxy.com hostname, which is "
            "what the TLS certificate covers."
        )
    if session_ids and not arguments.get("session_id"):
        notes.append(
            "Session ids were generated. Pass one back as session_id to keep using that "
            "IP, or to rotate_proxy_session to change it."
        )

    return _json(
        {
            "product": product,
            "name": PRODUCTS[product]["name"],
            "protocol": protocol,
            "host": host,
            "port": port,
            "username": username,
            "proxies": proxies,
            "session_ids": session_ids or None,
            # Built from the first entry's password, so the command verifies
            # proxies[0]. With a generated session every entry has its own.
            "curl_example": curl_example(
                scheme, host, port, username, first_password or base_password
            ),
            "notes": notes or None,
            "contains_credentials": True,
        }
    )


def _build_static_strings(
    products: dict[str, Any],
    entry: dict[str, Any],
    arguments: dict[str, Any],
    host: str,
    port: int,
    scheme: str,
    template: str,
    count: int,
) -> str:
    """Static Residential: the exit IP is chosen by prefixing it to the password."""
    ips = _static_ips(entry)
    if not ips:
        raise ProxyOptionError(
            "This account rents no Static Residential IPs."
        )

    wanted = arguments.get("static_ip")
    if wanted:
        ips = [ip for ip in ips if ip["ip"] == wanted]
        if not ips:
            raise ProxyOptionError(
                f"'{wanted}' is not one of this account's Static Residential IPs."
            )

    # Reject targeting options that silently do nothing on this product.
    build_password_modifiers(
        "static_residential",
        countries=arguments.get("countries"),
        region=arguments.get("region"),
        city=arguments.get("city"),
        session=arguments.get("session"),
    )

    # GET /public reports Static Residential without a username; the account's
    # proxy username is the same one the rotating products use.
    username = entry.get("username") or _account_username(products)

    selected = ips if wanted else ips[:count]
    proxies: list[dict[str, Any]] = []
    first_password: str | None = None
    for ip in selected:
        if not ip["ip"] or not ip["password"]:
            continue
        password = f"{ip['ip']}_{ip['password']}"
        if first_password is None:
            first_password = password
        proxies.append(
            {
                "ip": ip["ip"],
                "proxy": format_proxy(
                    template, scheme, host, port, username or "USERNAME", password
                ),
                "expiry_date": ip["expiry_date"],
            }
        )

    return _json(
        {
            "product": "static_residential",
            "name": PRODUCTS["static_residential"]["name"],
            "protocol": "socks5" if scheme == "socks5" else "http",
            "host": host,
            "port": port,
            "username": username,
            "proxies": proxies,
            # Same key as the rotating path, for the same reason: the two
            # responses differ in which per-entry fields they carry, not in
            # their shape.
            "curl_example": (
                curl_example(
                    scheme, host, port, username or "USERNAME", first_password
                )
                if first_password
                else None
            ),
            "notes": [
                "The exit IP is selected by the '<ip>_' prefix on the password; "
                "geo targeting and sessions do not apply.",
            ]
            + (
                []
                if entry.get("endpoint") or arguments.get("gateway_host")
                else [
                    f"The API reports no gateway hostname for Static Residential, so "
                    f"'{host}' is the documented default. Gateways are assigned per "
                    "account — check the host shown next to the IPs in the dashboard "
                    "and pass gateway_host if it differs."
                ]
            )
            + (
                []
                if username
                else [
                    "The API did not report a username for Static Residential — use the "
                    "proxy username from list_proxy_products in place of USERNAME."
                ]
            ),
            "contains_credentials": True,
        }
    )


async def _generate_proxy_list(
    client: EvomiPublicClient, arguments: dict[str, Any]
) -> str:
    # Refused before the request is sent, not after: there is no reason to have
    # the API mint a list of credentials that this server is then going to throw
    # away, and it keeps the bulk material from existing at all.
    if passwords_hidden():
        raise _refuse_hidden(
            "generate_proxy_list",
            "Its entire output is proxy lines with the account's password in "
            "each one, so there is nothing left to return once they are masked.",
        )

    product = arguments["product"]
    if product not in PRODUCTS:
        raise ProxyOptionError(
            f"Unknown product '{product}'. Valid products: {', '.join(PRODUCT_CODES)}."
        )

    countries = arguments.get("countries")
    params = {
        "product": PRODUCTS[product]["generate_code"],
        "amount": _bounded_count(arguments.get("amount", 1), "amount"),
        "format": arguments.get("format", "1"),
        "protocol": arguments.get("protocol", "http"),
        "prepend_protocol": "true" if arguments.get("prepend_protocol", True) else "false",
        "countries": ",".join(c.strip().upper() for c in countries) if countries else None,
        "city": arguments.get("city"),
        "region": arguments.get("region"),
        "isp": arguments.get("isp"),
        "session": arguments.get("session"),
        "lifetime": arguments.get("lifetime_minutes"),
    }
    if arguments.get("adblock"):
        params["adblock"] = "true"

    text = await client.generate_proxies(**params)
    lines = [line for line in text.splitlines() if line.strip()]

    return _json(
        {
            "product": product,
            "count": len(lines),
            "proxies": lines,
            "contains_credentials": True,
        }
    )


async def _get_proxy_usage(client: EvomiPublicClient, arguments: dict[str, Any]) -> str:
    product = arguments["product"]
    period = arguments.get("period", "3d")

    if period not in USAGE_PERIODS:
        raise ProxyOptionError(f"period must be one of: {', '.join(USAGE_PERIODS)}.")

    payload = await client.get_usage(product=product, period=period)
    meta = payload.get("meta") or {}
    products = ((payload.get("data") or {}).get("bandwidth") or {}).get("products") or []

    entry = products[0] if products else {}
    stats = entry.get("stats") or {}
    buckets = sorted(stats.items()) if isinstance(stats, dict) else []

    max_buckets = max(1, min(int(arguments.get("max_buckets", 24)), 500))
    include_buckets = arguments.get("include_buckets", True)

    total_mb = _round_mb(meta.get("total_bandwidth_mb") or entry.get("totalBandwidth"))

    result: dict[str, Any] = {
        "product": product,
        "name": PRODUCTS.get(product, {}).get("name"),
        "period": meta.get("period", period),
        "granularity": meta.get("granularity"),
        "total_bandwidth_mb": total_mb,
        "total_bandwidth_gb": round(total_mb / 1000, 3) if total_mb is not None else None,
        "bucket_count": len(buckets),
    }

    if include_buckets and buckets:
        trimmed = buckets[-max_buckets:]
        result["buckets_returned"] = len(trimmed)
        result["buckets"] = [
            {"time": when, **({"total_mb": _round_mb(value.get("total"))} if isinstance(value, dict) else {"total_mb": _round_mb(value)})}
            for when, value in trimmed
        ]

    return _json(result)


def _normalise_options(raw: Any) -> list[dict[str, Any]]:
    """
    Flatten the several shapes GET /public/settings uses into name/value pairs.

    The payload nests differently per field — a code-to-name map for countries, a
    `{"data": [...]}` wrapper for cities and regions, objects carrying `value`
    and `countryCode` for ISPs — so normalise rather than assume.
    """
    if raw is None:
        return []

    if isinstance(raw, dict) and isinstance(raw.get("data"), (list, dict)):
        raw = raw["data"]

    options: list[dict[str, Any]] = []

    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                option = {"name": key}
                for field in ("value", "countryCode", "code", "name"):
                    if field in value and not isinstance(value[field], (dict, list)):
                        option[field] = value[field]
                options.append(option)
            elif isinstance(value, list):
                options.append({"name": key, "count": len(value)})
            else:
                options.append({"name": key, "value": value})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                options.append(
                    {
                        k: v
                        for k, v in item.items()
                        if not isinstance(v, (dict, list))
                    }
                    or {"name": str(item)}
                )
            else:
                options.append({"name": str(item)})

    return options


async def _list_targeting_options(
    client: EvomiPublicClient, arguments: dict[str, Any]
) -> str:
    product = arguments["product"]
    kind = arguments.get("kind", "countries")
    search = (arguments.get("search") or "").strip().lower()
    limit = max(1, min(int(arguments.get("limit", 50)), 500))

    payload = await client.get_settings()
    product_settings = (payload.get("data") or {}).get(product)

    if not isinstance(product_settings, dict):
        raise ProxyOptionError(
            f"The Public API reported no targeting options for product '{product}'."
        )

    field = {
        "countries": "countries",
        "regions": "regions",
        "cities": "cities",
        "isps": "isp",
        "continents": "continents",
    }[kind]

    options = _normalise_options(product_settings.get(field))

    if search:
        options = [
            option
            for option in options
            if any(search in str(value).lower() for value in option.values())
        ]

    return _json(
        {
            "product": product,
            "kind": kind,
            "search": search or None,
            "match_count": len(options),
            "returned": min(len(options), limit),
            "options": options[:limit],
            "note": "Use these values exactly as shown when targeting.",
        }
    )


async def _rotate_session(client: EvomiPublicClient, arguments: dict[str, Any]) -> str:
    product = arguments["product"]
    session_id = arguments["session_id"]

    spec = PRODUCTS.get(product)
    if not spec or not spec["rotate_code"]:
        raise ProxyOptionError(
            f"Sessions cannot be rotated on product '{product}'. "
            f"Rotatable products: {', '.join(ROTATABLE_PRODUCTS)}."
        )

    result = await client.rotate_session(session_id=session_id, product=spec["rotate_code"])

    return _json(
        {
            "product": product,
            "session_id": session_id,
            "rotated": bool(result.get("success")),
            "message": result.get("message") or result.get("error"),
        }
    )


async def _get_api_access(client: EvomiPublicClient, arguments: dict[str, Any]) -> str:
    service = arguments.get("service", "all")
    reveal = bool(arguments.get("include_api_key", False)) and not passwords_hidden()

    result: dict[str, Any] = {}

    if service in ("scraper", "all"):
        scraper = await client.get_scraper_access()
        result["scraper"] = {
            "has_access": scraper.get("has_access"),
            "credits": scraper.get("credits"),
            "concurrency": scraper.get("concurrency"),
            "endpoint_url": scraper.get("endpoint_url"),
            "api_key": scraper.get("api_key") if reveal else mask_secret(scraper.get("api_key")),
            "api_key_revealed": reveal,
        }

    if service in ("browser", "all"):
        browser = await client.get_browser_access()
        result["browser"] = {
            "has_access": browser.get("has_access"),
            "credits": browser.get("credits"),
            "concurrency": browser.get("concurrency"),
            "max_session_length_seconds": browser.get("max_session_length_seconds"),
            "endpoint_url": browser.get("endpoint_url"),
            "api_key": browser.get("api_key") if reveal else mask_secret(browser.get("api_key")),
            "api_key_revealed": reveal,
        }

    result["note"] = (
        "These are per-service keys, separate from the Public API key. The scraper "
        "key is what the scrape/crawl tools need in EVOMI_SCRAPER_API_KEY."
    )
    return _json(result)


def _profile_list(payload: dict[str, Any]) -> list[Any]:
    """The profile array out of a GET /public/profiles response."""
    profiles = payload.get("profiles")
    return profiles if isinstance(profiles, list) else []


async def _existing_profile_count(client: EvomiPublicClient) -> int | None:
    """
    How many browser profiles the account already has, or None if unknown.

    Only GET /public/profiles can say whether an order falls inside the free
    allowance, so it is read first. It has no side effects, and failing to count
    must not cancel an order the user explicitly asked for — hence None rather
    than letting the error propagate.
    """
    try:
        return len(_profile_list(await client.list_browser_profiles()))
    except EvomiPublicAPIError:
        return None


async def _order_browser_profile(
    client: EvomiPublicClient, arguments: dict[str, Any]
) -> str:
    # The tool's parameter is named `os`, so it is read out of `arguments`
    # rather than bound to a local name.
    os_name = str(arguments.get("os") or "").strip()
    browser_version = str(arguments.get("browser_version") or "").strip()

    if not os_name:
        raise ProxyOptionError(
            "os is required — the operating system the fingerprint should present. "
            "list_browser_profiles shows the values existing profiles use."
        )
    if not browser_version:
        raise ProxyOptionError(
            "browser_version is required — the browser version the fingerprint "
            "should present. list_browser_profiles shows the values existing "
            "profiles use."
        )

    existing = await _existing_profile_count(client)

    payload = await client.order_browser_profile(
        os_name=os_name, browser_version=browser_version
    )

    notes = [
        f"The first {FREE_BROWSER_PROFILES} profiles on an account are free; each "
        f"one after them costs {BROWSER_PROFILE_COST_GB} GB of data balance. What "
        "was actually charged is decided by the API, not by this tool.",
    ]
    if existing is None:
        notes.append(
            "The existing profile count could not be read, so whether this order "
            "was free is unknown. list_browser_profiles shows the current list."
        )

    return _json(
        {
            # The client raises on any non-2xx, so reaching this point means the
            # API accepted the order even if it sent no explicit success flag.
            "ordered": bool(payload.get("success", True)),
            "os": os_name,
            "browser_version": browser_version,
            "profile": payload.get("profile") or payload.get("data") or None,
            "message": payload.get("message") or payload.get("error"),
            "profiles_before_this_order": existing,
            "free_profile_allowance": FREE_BROWSER_PROFILES,
            "expected_cost_gb": (
                None
                if existing is None
                else BROWSER_PROFILE_COST_GB
                if existing >= FREE_BROWSER_PROFILES
                else 0
            ),
            "notes": notes,
        }
    )
