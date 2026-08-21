"""Credential handling: masking, scrubbing, and non-leaking auth failures."""

import os

import httpx
import pytest

from evomi_mcp.client import EvomiClient
from evomi_mcp.public_client import EvomiAuthError, EvomiPublicAPIError, EvomiPublicClient
from evomi_mcp.security import MASK, mask_secret, scrub


# ─── Masking ────────────────────────────────────────────────────────────────────


def test_mask_carries_no_characters_of_the_secret():
    secret = "abc123def456ghi"
    masked = mask_secret(secret)

    assert masked == MASK
    assert not any(part and part in masked for part in (secret, secret[-4:]))


def test_mask_is_the_same_length_whatever_the_secret():
    assert mask_secret("short") == mask_secret("a" * 200) == MASK


def test_absent_secret_stays_absent():
    assert mask_secret(None) is None
    assert mask_secret("") is None


# ─── Scrubbing ──────────────────────────────────────────────────────────────────


def test_configured_key_is_scrubbed_from_error_text(monkeypatch):
    monkeypatch.setenv("EVOMI_PUBLIC_API_KEY", "supersecretkey123")
    assert scrub("failed with supersecretkey123") == "failed with [redacted]"


def test_query_parameter_keys_are_scrubbed_even_when_unknown():
    text = "GET https://api.evomi.com/public?apikey=whatever-this-is failed"
    assert "whatever-this-is" not in scrub(text)


def test_caller_supplied_secrets_are_scrubbed():
    assert scrub("password rp_secret_pw leaked", ["rp_secret_pw"]) == "password [redacted] leaked"


def test_short_values_are_not_scrubbed_to_avoid_mangling_text(monkeypatch):
    monkeypatch.setenv("EVOMI_API_KEY", "abc")
    assert scrub("abcdef") == "abcdef"


# ─── Public API client authentication ───────────────────────────────────────────


def test_missing_key_names_the_variable_to_set(monkeypatch):
    monkeypatch.delenv("EVOMI_PUBLIC_API_KEY", raising=False)
    monkeypatch.delenv("EVOMI_API_KEY", raising=False)

    with pytest.raises(EvomiAuthError, match="EVOMI_PUBLIC_API_KEY"):
        EvomiPublicClient()


def test_falls_back_to_the_generic_key(monkeypatch):
    monkeypatch.delenv("EVOMI_PUBLIC_API_KEY", raising=False)
    monkeypatch.setenv("EVOMI_API_KEY", "generic-key")

    assert EvomiPublicClient().api_key == "generic-key"


def test_key_is_sent_as_a_header_and_never_in_the_url(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("x-apikey")
        return httpx.Response(200, json={"success": True})

    _patch_transport(monkeypatch, handler)

    client = EvomiPublicClient(api_key="header-only-key")
    import asyncio

    asyncio.run(client.get_proxy_products())

    assert seen["header"] == "header-only-key"
    assert "header-only-key" not in seen["url"]


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_produces_a_clear_non_leaking_error(monkeypatch, status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "Unauthorized"})

    _patch_transport(monkeypatch, handler)

    import asyncio

    client = EvomiPublicClient(api_key="rejected-key-value")
    with pytest.raises(EvomiAuthError) as excinfo:
        asyncio.run(client.get_proxy_products())

    message = str(excinfo.value)
    assert str(status) in message
    assert "rejected-key-value" not in message


def test_api_error_string_is_surfaced_but_scrubbed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "Invalid product for user"})

    _patch_transport(monkeypatch, handler)

    import asyncio

    client = EvomiPublicClient(api_key="k" * 20)
    with pytest.raises(EvomiPublicAPIError, match="Invalid product for user"):
        asyncio.run(client.get_proxy_products())


def test_transport_failure_does_not_expose_the_request(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_transport(monkeypatch, handler)

    import asyncio

    client = EvomiPublicClient(api_key="k" * 20)
    with pytest.raises(EvomiPublicAPIError) as excinfo:
        asyncio.run(client.get_proxy_products())

    assert "ConnectError" in str(excinfo.value)
    assert "api.evomi.com" not in str(excinfo.value)


# ─── Scraper client authentication ──────────────────────────────────────────────


@pytest.fixture
def no_scraper_key(monkeypatch):
    """Clear both scraper variables, so the ambient environment cannot pass a test."""
    monkeypatch.delenv("EVOMI_SCRAPER_API_KEY", raising=False)
    monkeypatch.delenv("EVOMI_API_KEY", raising=False)


def test_scraper_client_reads_the_documented_variable(monkeypatch, no_scraper_key):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "scraper-key")

    assert EvomiClient().api_key == "scraper-key"


def test_scraper_client_falls_back_to_the_generic_key(monkeypatch, no_scraper_key):
    monkeypatch.setenv("EVOMI_API_KEY", "generic-key")

    assert EvomiClient().api_key == "generic-key"


def test_specific_scraper_key_wins_over_the_generic_one(monkeypatch, no_scraper_key):
    """Precedence is decided here, not left to whichever getenv runs first."""
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "scraper-key")
    monkeypatch.setenv("EVOMI_API_KEY", "generic-key")

    assert EvomiClient().api_key == "scraper-key"


def test_configured_scraper_key_is_not_the_public_api_key(monkeypatch, no_scraper_key):
    """The two credentials come from different systems, so neither substitutes.

    EVOMI_PUBLIC_API_KEY is set by an autouse fixture; the scraper client must
    not treat it as its own key. Where no scraper key is configured the client
    asks the Public API for one instead — see test_scraper_client.py.
    """
    assert os.getenv("EVOMI_PUBLIC_API_KEY")

    assert EvomiClient().api_key is None


def _patch_transport(monkeypatch, handler):
    """Route every AsyncClient through a mock transport."""
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
