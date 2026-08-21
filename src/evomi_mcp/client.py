"""Evomi API client for the scraper API.

The scraper key is a different credential from the Public API key in
`public_client.py`, issued by a different system and sent as a different header.
When only the Public API key is configured, the scraper key is fetched from
`GET /public/scraper` on first use and held in memory for the process.
"""

import asyncio
import os
from typing import Any, Optional

import httpx

from .public_client import EvomiPublicAPIError, EvomiPublicClient
from .security import scrub

DEFAULT_BASE_URL = "https://scrape.evomi.com"

DASHBOARD_URL = "https://my.evomi.com/settings/api"

NO_CREDENTIAL_MESSAGE = (
    "No Evomi credential configured for the scraper API. Set EVOMI_SCRAPER_API_KEY "
    "to a scraper key, or set EVOMI_PUBLIC_API_KEY and the scraper key is fetched "
    f"from the Public API on first use. Both keys are at {DASHBOARD_URL}. "
    "EVOMI_API_KEY is used as a fallback for either."
)


class EvomiScraperAPIError(RuntimeError):
    """An Evomi scraper API call failed. The message never contains credentials."""


class EvomiScraperAuthError(EvomiScraperAPIError):
    """The scraper key is missing, invalid, or not permitted for this call."""


class EvomiClient:
    """HTTP client for Evomi scraper API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """
        Initialize the Evomi API client.

        Args:
            api_key: Evomi scraper API key. Defaults to EVOMI_SCRAPER_API_KEY,
                then EVOMI_API_KEY, then the key the Public API reports, fetched
                on the first call that needs it.
            base_url: API base URL (defaults to EVOMI_BASE_URL env var or
                https://scrape.evomi.com)
        """
        self.api_key = api_key or os.getenv("EVOMI_SCRAPER_API_KEY") or os.getenv("EVOMI_API_KEY")
        self.base_url = base_url or os.getenv("EVOMI_BASE_URL", DEFAULT_BASE_URL)

        self._fetched_key: Optional[str] = None
        self._key_lock = asyncio.Lock()

    # ─── Credentials ─────────────────────────────────────────────────────────────

    async def resolve_api_key(self) -> str:
        """
        The scraper key to authenticate with, fetching it once if it is not set.

        A configured key is used as-is and no request is made. Otherwise the
        Public API is asked for the account's scraper key and the answer is kept
        for the lifetime of this client. The lock makes concurrent first calls
        fetch once rather than once each.
        """
        if self.api_key:
            return self.api_key
        if self._fetched_key:
            return self._fetched_key

        async with self._key_lock:
            if self._fetched_key is None:
                self._fetched_key = await self._fetch_scraper_key()
            return self._fetched_key

    async def _fetch_scraper_key(self) -> str:
        """Read the account's scraper key from GET /public/scraper."""
        try:
            public = EvomiPublicClient()
        except EvomiPublicAPIError:
            raise EvomiScraperAuthError(NO_CREDENTIAL_MESSAGE) from None

        try:
            access = await public.get_scraper_access()
        except EvomiPublicAPIError as exc:
            raise EvomiScraperAuthError(
                f"Could not read the scraper key from the Evomi Public API: {exc} "
                "Set EVOMI_SCRAPER_API_KEY directly to skip this lookup."
            ) from None

        key = access.get("api_key") if isinstance(access, dict) else None
        if not key:
            raise EvomiScraperAuthError(
                "The Evomi Public API reports no scraper key for this account, so "
                "the scraping tools have nothing to authenticate with. Enable the "
                f"Scraper API at {DASHBOARD_URL}, or set EVOMI_SCRAPER_API_KEY to a "
                "key issued to another account."
            )
        return str(key)

    # ─── Requests ────────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
        credits: bool = False,
    ) -> Any:
        """
        Send one request to the scraper API and normalise its failure modes.

        Raises EvomiScraperAPIError with a message safe to show a user: it
        carries the status and, where the API supplied one, its own error
        strings — never the request URL, headers, or a raw body.
        """
        api_key = await self.resolve_api_key()
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise EvomiScraperAPIError(
                f"Could not reach the Evomi Scraper API ({type(exc).__name__})."
            ) from None

        payload = _json_or_none(response)
        self._raise_for_status(response, payload, api_key)

        if payload is None:
            raise EvomiScraperAPIError(
                f"Evomi Scraper API returned a non-JSON response "
                f"(HTTP {response.status_code})."
            )

        if credits and isinstance(payload, dict):
            payload["_credits_used"] = response.headers.get("X-Credits-Used")
            payload["_credits_remaining"] = response.headers.get("X-Credits-Remaining")
            payload["_mode_used"] = response.headers.get("X-Mode-Used")

        return payload

    def _raise_for_status(
        self, response: httpx.Response, payload: Any, api_key: str
    ) -> None:
        status = response.status_code
        if status < 400:
            return

        # 408 is how the API hands a slow request over to the async task queue:
        # the body carries the task id to poll, so it is a success.
        if status == 408 and isinstance(payload, dict) and payload.get("task_id"):
            return

        detail = _error_detail(payload, api_key)

        if status in (401, 403):
            raise EvomiScraperAuthError(
                f"Evomi Scraper API rejected the API key (HTTP {status}){detail} "
                f"Set EVOMI_SCRAPER_API_KEY to a current key from {DASHBOARD_URL}, "
                "or set EVOMI_PUBLIC_API_KEY and the key is fetched for you."
            )

        if status == 402:
            raise EvomiScraperAPIError(
                f"Evomi Scraper API reports insufficient credits (HTTP 402){detail} "
                f"Top the account up at {DASHBOARD_URL}, or use a cheaper mode."
            )

        if status == 429:
            raise EvomiScraperAPIError(
                f"Evomi Scraper API rate limit reached (HTTP 429){detail} "
                "Wait for the concurrency limit to clear, or run the call with "
                "async_mode set and poll get_task_status."
            )

        if status >= 500:
            raise EvomiScraperAPIError(
                f"Evomi Scraper API is unavailable (HTTP {status}){detail} "
                "Retry with backoff."
            )

        raise EvomiScraperAPIError(
            f"Evomi Scraper API rejected the request (HTTP {status}){detail}"
        )

    # ─── Scraping Operations ────────────────────────────────────────────────────

    async def scrape(
        self,
        url: str,
        mode: str = "auto",
        output: str = "markdown",
        device: str = "windows",
        proxy_type: str = "residential",
        proxy_country: str = "US",
        proxy_session_id: Optional[str] = None,
        wait_until: str = "domcontentloaded",
        ai_enhance: bool = False,
        ai_prompt: Optional[str] = None,
        ai_source: Optional[str] = None,
        ai_force_json: bool = True,
        js_instructions: Optional[list[dict]] = None,
        execute_js: Optional[str] = None,
        block_resources: Optional[list[str]] = None,
        screenshot: bool = False,
        pdf: bool = False,
        wait_seconds: int = 0,
        excluded_tags: Optional[list[str]] = None,
        excluded_selectors: Optional[list[str]] = None,
        additional_headers: Optional[dict[str, str]] = None,
        capture_headers: bool = False,
        network_capture: Optional[list[dict]] = None,
        async_mode: bool = False,
        include_content: bool = True,
        delivery: str = "json",
        config_id: Optional[str] = None,
        scheme_id: Optional[str] = None,
        extract_scheme: Optional[list[dict]] = None,
        storage_id: Optional[str] = None,
        use_default_storage: bool = False,
        no_html: bool = False,
    ) -> dict[str, Any]:
        """
        Scrape a single URL.

        Args:
            url: URL to scrape
            mode: Scraping mode - "request", "browser", or "auto"
            output: Output format - "html", "markdown", "screenshot", or "pdf"
            device: Device type to emulate - "windows", "macos", or "android"
            proxy_type: Proxy type - "datacenter" or "residential"
            proxy_country: Two-letter country code for proxy
            proxy_session_id: Proxy session ID (6-8 characters)
            wait_until: Wait condition - "load", "domcontentloaded", "networkidle", or "commit"
            ai_enhance: Enable AI enhancement
            ai_prompt: Prompt for AI extraction
            ai_source: Source for AI - "markdown" or "screenshot"
            ai_force_json: Force AI response to be valid JSON (default: True)
            js_instructions: List of JS instructions (click, wait, fill, wait_for)
            execute_js: Raw JavaScript code to execute
            block_resources: Resource types to block
            screenshot: Capture screenshot
            pdf: Capture PDF
            wait_seconds: Seconds to wait after page load
            excluded_tags: HTML tags to remove before processing
            excluded_selectors: CSS selectors to remove
            additional_headers: Extra headers to send with request
            capture_headers: Capture response headers
            network_capture: Network capture filters
            async_mode: Return immediately with task ID
            include_content: Include content in JSON response
            delivery: Response format - "raw" or "json"
            config_id: Saved config ID to use
            scheme_id: Saved extraction scheme ID
            extract_scheme: Extraction scheme for structured data
            storage_id: Storage config ID for results
            use_default_storage: Use default storage config
            no_html: Exclude HTML from JSON response

        Returns:
            Scraping result with content, credits used, etc.
        """
        payload: dict[str, Any] = {
            "url": url,
            "mode": mode,
            "content": output,
            "device": device,
            "proxy_type": proxy_type,
            "proxy_country": proxy_country,
            "wait_until": wait_until,
            "ai_enhance": ai_enhance,
            "screenshot": screenshot,
            "pdf": pdf,
            "wait_seconds": wait_seconds,
            "async": async_mode,
            "include_content": include_content,
            "delivery": delivery,
            "capture_headers": capture_headers,
            "use_default_storage": use_default_storage,
            "no_html": no_html,
        }

        if proxy_session_id:
            payload["proxy_session_id"] = proxy_session_id

        if ai_enhance and ai_prompt:
            payload["ai_prompt"] = ai_prompt
            payload["ai_source"] = ai_source or "markdown"
        elif ai_source:
            payload["ai_source"] = ai_source

        if ai_enhance:
            payload["ai_force_json"] = ai_force_json

        if js_instructions:
            payload["js_instructions"] = js_instructions

        if execute_js:
            payload["execute_js"] = execute_js

        if block_resources:
            payload["block_resources"] = block_resources

        if excluded_tags:
            payload["excluded_tags"] = excluded_tags

        if excluded_selectors:
            payload["excluded_selectors"] = excluded_selectors

        if additional_headers:
            payload["additional_headers"] = additional_headers

        if network_capture:
            payload["networkCapture"] = network_capture

        if config_id:
            payload["config_id"] = config_id

        if scheme_id:
            payload["scheme_id"] = scheme_id

        if extract_scheme:
            payload["extract_scheme"] = extract_scheme

        if storage_id:
            payload["storage_id"] = storage_id

        return await self._request(
            "POST",
            "/api/v1/scraper/realtime",
            json_body=payload,
            timeout=120.0,
            credits=True,
        )

    async def crawl(
        self,
        domain: str,
        max_urls: int = 100,
        depth: int = 2,
        url_pattern: Optional[str] = None,
        scraper_config: Optional[dict[str, Any]] = None,
        async_mode: bool = False,
    ) -> dict[str, Any]:
        """
        Crawl a website starting from a domain.

        Args:
            domain: Domain to crawl
            max_urls: Maximum URLs to crawl
            depth: Crawl depth
            url_pattern: Regex pattern to filter URLs
            scraper_config: Configuration for scraping each page
            async_mode: Return immediately with task ID

        Returns:
            Crawl results with discovered URLs and content
        """
        payload: dict[str, Any] = {
            "domain": domain,
            "max_urls": max_urls,
            "depth": depth,
            "async": async_mode,
        }

        if url_pattern:
            payload["url_pattern"] = url_pattern

        if scraper_config:
            payload["scraper_config"] = scraper_config

        return await self._request(
            "POST",
            "/api/v1/scraper/crawl",
            json_body=payload,
            timeout=300.0,
            credits=True,
        )

    async def map_website(
        self,
        domain: str,
        sources: Optional[list[str]] = None,
        max_urls: int = 500,
        url_pattern: Optional[str] = None,
        check_if_live: bool = False,
        depth: int = 1,
        async_mode: bool = False,
    ) -> dict[str, Any]:
        """
        Discover URLs from a website.

        Args:
            domain: Domain to map
            sources: Discovery sources - "sitemap", "commoncrawl", "crawl"
            max_urls: Maximum URLs to discover
            url_pattern: Regex pattern to filter URLs
            check_if_live: Check if URLs are live
            depth: Crawl depth if using crawl source
            async_mode: Return immediately with task ID

        Returns:
            Map results with discovered URLs
        """
        if sources is None:
            sources = ["sitemap", "commoncrawl"]

        payload: dict[str, Any] = {
            "domain": domain,
            "sources": sources,
            "max_urls": max_urls,
            "check_if_live": check_if_live,
            "depth": depth,
            "async": async_mode,
        }

        if url_pattern:
            payload["url_pattern"] = url_pattern

        return await self._request(
            "POST",
            "/api/v1/scraper/map",
            json_body=payload,
            timeout=300.0,
            credits=True,
        )

    async def search_domains(
        self,
        query: str | list[str],
        max_urls: int = 20,
        region: str = "us-en",
    ) -> dict[str, Any]:
        """
        Find domains by searching the web.

        Args:
            query: Search query (string or list of up to 10 queries)
            max_urls: Maximum domains to return per query (default: 20, max: 100)
            region: Region for search results and proxy location (e.g., "us-en", "de-de")

        Returns:
            Search results with list of domains, each containing domain, url, and title
        """
        payload: dict[str, Any] = {
            "query": query,
            "max_urls": min(max_urls, 100),
            "region": region,
        }

        return await self._request(
            "POST",
            "/api/v1/scraper/search",
            json_body=payload,
            timeout=60.0,
            credits=True,
        )

    async def agent_request(
        self,
        message: str,
    ) -> dict[str, Any]:
        """
        Send a request to the AI agent for scraping assistance.

        Args:
            message: Natural language request for the agent

        Returns:
            Agent response with actions taken
        """
        return await self._request(
            "POST",
            "/api/v1/agent/request",
            json_body={"message": message},
            timeout=180.0,
            credits=True,
        )

    async def get_task_status(
        self,
        task_id: str,
        task_type: str = "scrape",
    ) -> dict[str, Any]:
        """
        Get the status of an async task.

        Args:
            task_id: Task ID to check
            task_type: Type of task - "scrape", "crawl", "map", "config_generate", or "schema"

        Returns:
            Task status and results if completed
        """
        if task_type == "crawl":
            path = f"/api/v1/scraper/crawl/tasks/{task_id}"
        elif task_type == "map":
            path = f"/api/v1/scraper/map/tasks/{task_id}"
        elif task_type == "config_generate":
            path = f"/api/v1/account/configs/generate/tasks/{task_id}"
        elif task_type == "schema":
            # Schema progress is keyed by scheme_id rather than by a task id.
            path = f"/api/v1/account/schemes/{task_id}/status"
        else:
            path = f"/api/v1/scraper/tasks/{task_id}"

        return await self._request("GET", path, credits=True)

    # ─── Config Management ──────────────────────────────────────────────────────

    async def list_configs(
        self,
        page: int = 1,
        per_page: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """List all scrape configs for the authenticated user."""
        return await self._request(
            "GET",
            "/api/v1/account/configs",
            params={
                "page": page,
                "per_page": per_page,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
        )

    async def create_config(
        self,
        name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a new scrape config."""
        return await self._request(
            "POST",
            "/api/v1/account/configs",
            json_body={"name": name, "config": config},
        )

    async def get_config(self, config_id: str) -> dict[str, Any]:
        """Get a single scrape config by ID."""
        return await self._request("GET", f"/api/v1/account/configs/{config_id}")

    async def update_config(
        self,
        config_id: str,
        name: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Update an existing scrape config."""
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if config:
            payload["config"] = config

        return await self._request(
            "PUT", f"/api/v1/account/configs/{config_id}", json_body=payload
        )

    async def delete_config(self, config_id: str) -> dict[str, Any]:
        """Delete a scrape config."""
        return await self._request("DELETE", f"/api/v1/account/configs/{config_id}")

    async def generate_config(
        self,
        name: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Generate a scrape config from a natural language prompt using AI."""
        return await self._request(
            "POST",
            "/api/v1/account/configs/generate",
            json_body={"name": name, "prompt": prompt},
            timeout=120.0,
            credits=True,
        )

    async def get_generate_config_status(self, task_id: str) -> dict[str, Any]:
        """Check the status of an async config generation task."""
        return await self._request(
            "GET", f"/api/v1/account/configs/generate/tasks/{task_id}"
        )

    # ─── Schema Management ──────────────────────────────────────────────────────

    async def list_schemas(
        self,
        page: int = 1,
        per_page: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """List all extraction schemas for the authenticated user."""
        return await self._request(
            "GET",
            "/api/v1/account/schemes",
            params={
                "page": page,
                "per_page": per_page,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
        )

    async def create_schema(
        self,
        name: str,
        config: dict[str, Any],
        test: bool = False,
        fix: bool = False,
        extract_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new extraction schema."""
        payload: dict[str, Any] = {
            "name": name,
            "config": config,
            "test": test,
            "fix": fix,
        }

        if extract_prompt:
            payload["extract_prompt"] = extract_prompt

        return await self._request(
            "POST",
            "/api/v1/account/schemes",
            json_body=payload,
            timeout=120.0,
            credits=True,
        )

    async def get_schema(self, scheme_id: str) -> dict[str, Any]:
        """Get a single extraction schema by ID."""
        return await self._request("GET", f"/api/v1/account/schemes/{scheme_id}")

    async def update_schema(
        self,
        scheme_id: str,
        name: str,
        config: dict[str, Any],
        test: bool = False,
        fix: bool = False,
        extract_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update an existing extraction schema."""
        payload: dict[str, Any] = {
            "name": name,
            "config": config,
            "test": test,
            "fix": fix,
        }

        if extract_prompt:
            payload["extract_prompt"] = extract_prompt

        return await self._request(
            "PUT",
            f"/api/v1/account/schemes/{scheme_id}",
            json_body=payload,
            timeout=120.0,
            credits=True,
        )

    async def delete_schema(self, scheme_id: str) -> dict[str, Any]:
        """Delete an extraction schema."""
        return await self._request("DELETE", f"/api/v1/account/schemes/{scheme_id}")

    async def get_schema_status(self, scheme_id: str) -> dict[str, Any]:
        """Get status of schema test."""
        return await self._request(
            "GET", f"/api/v1/account/schemes/{scheme_id}/status"
        )

    # ─── Storage Management ─────────────────────────────────────────────────────

    async def list_storage_configs(self) -> list[dict[str, Any]]:
        """List all storage configurations for the authenticated user."""
        return await self._request("GET", "/api/v1/account/storage")

    async def create_storage_config(
        self,
        name: str,
        storage_type: str,
        config: dict[str, Any],
        set_as_default: bool = False,
    ) -> dict[str, Any]:
        """Create a new storage configuration."""
        return await self._request(
            "POST",
            "/api/v1/account/storage",
            json_body={
                "name": name,
                "storage_type": storage_type,
                "config": config,
                "set_as_default": set_as_default,
            },
            timeout=60.0,
        )

    async def update_storage_config(
        self,
        storage_id: str,
        name: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
        set_as_default: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Update an existing storage configuration."""
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if config:
            payload["config"] = config
        if set_as_default is not None:
            payload["set_as_default"] = set_as_default

        return await self._request(
            "PUT",
            f"/api/v1/account/storage/{storage_id}",
            json_body=payload,
            timeout=60.0,
        )

    async def delete_storage_config(self, storage_id: str) -> dict[str, Any]:
        """Delete a storage configuration."""
        return await self._request("DELETE", f"/api/v1/account/storage/{storage_id}")

    # ─── Account Info ────────────────────────────────────────────────────────────

    async def get_account_info(self) -> dict[str, Any]:
        """Get user's account info including credit balance."""
        return await self._request("GET", "/api/v1/scraper/health")

    # ─── Schedule Management ────────────────────────────────────────────────────

    async def list_schedules(
        self,
        page: int = 1,
        per_page: int = 20,
        active_only: bool = False,
    ) -> dict[str, Any]:
        """List all scheduled jobs for the authenticated user."""
        return await self._request(
            "GET",
            "/api/v1/account/schedule",
            params={
                "page": page,
                "per_page": per_page,
                "active_only": str(active_only).lower(),
            },
        )

    async def create_schedule(
        self,
        name: str,
        config_id: str,
        interval_minutes: int,
        start_time: Optional[str] = None,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Create a new scheduled scrape job."""
        payload: dict[str, Any] = {
            "name": name,
            "config_id": config_id,
            "interval_minutes": interval_minutes,
            "stop_on_error": stop_on_error,
        }

        if start_time:
            payload["start_time"] = start_time

        return await self._request(
            "POST", "/api/v1/account/schedule", json_body=payload
        )

    async def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        """Get a single scheduled job by ID."""
        return await self._request("GET", f"/api/v1/account/schedule/{schedule_id}")

    async def update_schedule(
        self,
        schedule_id: str,
        name: Optional[str] = None,
        config_id: Optional[str] = None,
        interval_minutes: Optional[int] = None,
        stop_on_error: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Update an existing scheduled job."""
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if config_id:
            payload["config_id"] = config_id
        if interval_minutes:
            payload["interval_minutes"] = interval_minutes
        if stop_on_error is not None:
            payload["stop_on_error"] = stop_on_error

        return await self._request(
            "PUT", f"/api/v1/account/schedule/{schedule_id}", json_body=payload
        )

    async def delete_schedule(self, schedule_id: str) -> dict[str, Any]:
        """Delete a scheduled job."""
        return await self._request("DELETE", f"/api/v1/account/schedule/{schedule_id}")

    async def toggle_schedule(self, schedule_id: str) -> dict[str, Any]:
        """Toggle a scheduled job active/inactive."""
        return await self._request(
            "POST", f"/api/v1/account/schedule/{schedule_id}/toggle"
        )

    async def list_schedule_runs(
        self,
        schedule_id: str,
        page: int = 1,
        per_page: int = 20,
    ) -> dict[str, Any]:
        """Get execution history for a scheduled job."""
        return await self._request(
            "GET",
            f"/api/v1/account/schedule/{schedule_id}/runs",
            params={"page": page, "per_page": per_page},
        )


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_detail(payload: Any, api_key: str) -> str:
    """
    The API's own error strings, if it sent any, scrubbed.

    A rejected request states why it was rejected one level down, under
    `error.details.validation_errors`, while the top of the body says only
    'Invalid request parameters'. Both are collected, because the nested line is
    the one that names the offending field.
    """
    if not isinstance(payload, dict):
        return "."

    parts: list[str] = []

    for field in ("error", "message", "detail"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, dict):
            parts.extend(_nested_error_strings(value))

    unique = [part for part in dict.fromkeys(parts) if part]
    if not unique:
        return "."

    return ": " + scrub(" — ".join(unique), extra_secrets=(api_key,)) + "."


def _nested_error_strings(error: dict[str, Any]) -> list[str]:
    """Pull the readable lines out of a structured error object."""
    parts: list[str] = []

    for field in ("message", "error", "reason"):
        value = error.get(field)
        if isinstance(value, str) and value:
            parts.append(value)

    details = error.get("details")
    if isinstance(details, dict):
        for value in details.values():
            if isinstance(value, str) and value:
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(str(item).strip(" :") for item in value if item)
    elif isinstance(details, str) and details:
        parts.append(details)

    return parts
