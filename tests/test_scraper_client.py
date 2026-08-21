"""The scraper client: where its key comes from, and how failures surface.

Two guarantees are pinned here.

A scraper key set in the environment is used as-is. Where none is set, the
client asks the Public API for the account's scraper key on the first call that
needs one, keeps it for the process, and says what to set if that cannot be
done. The key is never part of an error message or a tool result.

Every non-2xx answer from the scraper API raises. An API that reports failures
in a 200 body is indistinguishable from one reporting data, so a status class
that fell through would reach a model as a successful result.
"""

import asyncio
import json

import httpx
import pytest

from evomi_mcp.client import (
    DASHBOARD_URL,
    EvomiClient,
    EvomiScraperAPIError,
    EvomiScraperAuthError,
)

SCRAPER_KEY = "sk-derived-0123456789abcdef"

# The Public API path the scraper key is read from, and the scraper API path a
# `scrape()` lands on.
KEY_PATH = "/public/scraper"
SCRAPE_PATH = "/api/v1/scraper/realtime"


def is_error(result) -> bool:
    """mcp 2.x renamed `isError` to `is_error`, keeping the old spelling as a wire alias."""
    return result.is_error if hasattr(result, "is_error") else result.isError


@pytest.fixture(autouse=True)
def no_configured_scraper_key(monkeypatch):
    """Clear both scraper variables, so the ambient environment cannot pass a test."""
    monkeypatch.delenv("EVOMI_SCRAPER_API_KEY", raising=False)
    monkeypatch.delenv("EVOMI_API_KEY", raising=False)


def _transport(monkeypatch, handler):
    """Route every AsyncClient through a mock transport."""
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _routes(monkeypatch, *, scraper_access=None, scrape=None, calls=None):
    """Serve the Public API's /scraper and the scraper API's /scrape."""
    scraper_access = scraper_access or (200, {"has_access": True, "api_key": SCRAPER_KEY})
    scrape = scrape or (200, {"content": "ok"})
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, body = scraper_access if request.url.path == KEY_PATH else scrape
        if isinstance(body, Exception):
            raise body
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    _transport(monkeypatch, handler)
    return calls


# ─── Where the key comes from ───────────────────────────────────────────────────


def test_configured_key_is_used_without_asking_the_public_api(monkeypatch):
    calls = _routes(monkeypatch)
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")

    client = EvomiClient()
    asyncio.run(client.scrape("https://example.com"))

    assert [c.url.path for c in calls] == [SCRAPE_PATH]
    assert calls[0].headers["x-api-key"] == "configured-key"


def test_key_is_fetched_from_the_public_api_when_none_is_configured(monkeypatch):
    calls = _routes(monkeypatch)

    client = EvomiClient()
    asyncio.run(client.scrape("https://example.com"))

    assert [c.url.path for c in calls] == [KEY_PATH, SCRAPE_PATH]
    assert calls[1].headers["x-api-key"] == SCRAPER_KEY


def test_the_fetch_happens_once_and_is_cached(monkeypatch):
    calls = _routes(monkeypatch)

    client = EvomiClient()

    async def three_calls():
        for _ in range(3):
            await client.scrape("https://example.com")

    asyncio.run(three_calls())

    assert [c.url.path for c in calls].count(KEY_PATH) == 1


def test_concurrent_first_calls_fetch_the_key_once(monkeypatch):
    calls = _routes(monkeypatch)

    client = EvomiClient()

    async def ten_at_once():
        await asyncio.gather(*(client.scrape("https://example.com") for _ in range(10)))

    asyncio.run(ten_at_once())

    assert [c.url.path for c in calls].count(KEY_PATH) == 1


def test_nothing_is_fetched_until_a_call_needs_the_key(monkeypatch):
    calls = _routes(monkeypatch)

    EvomiClient()

    assert calls == []


def test_no_credential_at_all_names_both_variables_and_the_dashboard(monkeypatch):
    _routes(monkeypatch)
    monkeypatch.delenv("EVOMI_PUBLIC_API_KEY", raising=False)

    client = EvomiClient()
    with pytest.raises(EvomiScraperAuthError) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    message = str(excinfo.value)
    assert "EVOMI_SCRAPER_API_KEY" in message
    assert "EVOMI_PUBLIC_API_KEY" in message
    assert DASHBOARD_URL in message


def test_a_rejected_public_key_is_reported_not_worked_around(monkeypatch):
    calls = _routes(monkeypatch, scraper_access=(401, {"error": "Unauthorized"}))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAuthError) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    # No unauthenticated attempt after the lookup failed.
    assert [c.url.path for c in calls] == [KEY_PATH]
    assert "EVOMI_SCRAPER_API_KEY" in str(excinfo.value)


def test_an_account_without_the_scraper_api_is_told_to_enable_it(monkeypatch):
    _routes(monkeypatch, scraper_access=(200, {"has_access": False, "api_key": None}))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAuthError) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    assert DASHBOARD_URL in str(excinfo.value)


def test_a_failed_lookup_does_not_cache_and_a_later_call_retries(monkeypatch):
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == KEY_PATH:
            if state["fail"]:
                return httpx.Response(503, json={"error": "upstream"})
            return httpx.Response(200, json={"api_key": SCRAPER_KEY})
        return httpx.Response(200, json={"content": "ok"})

    _transport(monkeypatch, handler)

    client = EvomiClient()
    with pytest.raises(EvomiScraperAuthError):
        asyncio.run(client.scrape("https://example.com"))

    state["fail"] = False
    assert asyncio.run(client.scrape("https://example.com"))["content"] == "ok"


def test_a_derived_key_never_appears_in_an_error(monkeypatch):
    _routes(
        monkeypatch,
        scrape=(400, {"error": f"bad request for key {SCRAPER_KEY}"}),
    )

    client = EvomiClient()
    with pytest.raises(EvomiScraperAPIError) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    assert SCRAPER_KEY not in str(excinfo.value)


def test_a_derived_key_never_appears_in_a_tool_result(monkeypatch):
    from evomi_mcp import server

    _routes(monkeypatch, scrape=(200, {"content": "ok"}))
    monkeypatch.setattr(server, "get_client", lambda: EvomiClient())

    result = asyncio.run(server.tool_result("scrape_url", {"url": "https://example.com"}))
    body = "".join(block.text for block in result.content)

    assert is_error(result) is False
    assert SCRAPER_KEY not in body


# ─── How failures surface ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected", "exc"),
    [
        (401, "rejected the API key", EvomiScraperAuthError),
        (403, "rejected the API key", EvomiScraperAuthError),
        (402, "insufficient credits", EvomiScraperAPIError),
        (429, "rate limit", EvomiScraperAPIError),
        (400, "rejected the request", EvomiScraperAPIError),
        (404, "rejected the request", EvomiScraperAPIError),
        (500, "unavailable", EvomiScraperAPIError),
        (503, "unavailable", EvomiScraperAPIError),
    ],
)
def test_each_status_class_raises_with_an_actionable_message(
    monkeypatch, status, expected, exc
):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(status, {"error": "Invalid API key"}))

    client = EvomiClient()
    with pytest.raises(exc) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    message = str(excinfo.value)
    assert expected in message
    assert str(status) in message


def test_the_apis_own_error_string_is_carried_through(monkeypatch):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(400, {"error": "url must be absolute"}))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAPIError, match="url must be absolute"):
        asyncio.run(client.scrape("https://example.com"))


def test_an_error_body_is_never_returned_as_data(monkeypatch):
    """A 4xx with a JSON body is the shape that reads as a successful payload."""
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(401, {"error": "Invalid API key"}))

    client = EvomiClient()
    for call in (
        client.get_account_info(),
        client.list_configs(),
        client.list_schemas(),
        client.list_schedules(),
        client.list_storage_configs(),
    ):
        with pytest.raises(EvomiScraperAPIError):
            asyncio.run(call)


def test_a_failing_scraper_call_sets_is_error(monkeypatch):
    from evomi_mcp import server

    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(401, {"error": "Invalid API key"}))
    monkeypatch.setattr(server, "get_client", lambda: EvomiClient())

    result = asyncio.run(server.tool_result("scrape_url", {"url": "https://example.com"}))

    assert is_error(result) is True
    assert "401" in result.content[0].text


def test_a_non_json_body_is_a_failure_not_a_result(monkeypatch):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(200, "<html>gateway</html>"))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAPIError, match="non-JSON"):
        asyncio.run(client.scrape("https://example.com"))


def test_a_transport_failure_names_the_fault_and_not_the_request(monkeypatch):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(0, httpx.ConnectError("connection refused")))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAPIError) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    message = str(excinfo.value)
    assert "ConnectError" in message
    assert "scrape.evomi.com" not in message


def test_408_carrying_a_task_id_is_an_async_handoff_not_an_error(monkeypatch):
    """The API answers a slow request with 408 and the id to poll."""
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(408, {"task_id": "t-1", "status": "pending"}))

    client = EvomiClient()

    assert asyncio.run(client.scrape("https://example.com"))["task_id"] == "t-1"


def test_408_without_a_task_id_is_an_error(monkeypatch):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(408, {"error": "timed out"}))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAPIError, match="408"):
        asyncio.run(client.scrape("https://example.com"))


def test_credit_headers_are_attached_to_a_scrape(monkeypatch):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": "ok"},
            headers={"X-Credits-Used": "2", "X-Credits-Remaining": "998"},
        )

    _transport(monkeypatch, handler)

    result = asyncio.run(EvomiClient().scrape("https://example.com"))

    assert result["_credits_used"] == "2"
    assert result["_credits_remaining"] == "998"


def test_the_error_message_is_json_safe_for_a_tool_result(monkeypatch):
    monkeypatch.setenv("EVOMI_SCRAPER_API_KEY", "configured-key")
    _routes(monkeypatch, scrape=(402, {"error": "no credits"}))

    client = EvomiClient()
    with pytest.raises(EvomiScraperAPIError) as excinfo:
        asyncio.run(client.scrape("https://example.com"))

    assert json.loads(json.dumps({"error": str(excinfo.value)}))
