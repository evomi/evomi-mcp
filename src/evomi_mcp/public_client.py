"""Evomi Public API client — proxy products, credentials, usage and targeting.

This is a different API and a different credential from the scraper client in
`client.py`: the Public API lives on api.evomi.com and authenticates with the
key from https://my.evomi.com/settings/api (personal) or Settings > Team (team),
sent as `x-apikey`. The scraper API lives on scrape.evomi.com and authenticates
with a separate key, sent as `x-api-key`.

The key is only ever sent as a header. The API also accepts `?apikey=`, which is
not used here, so a credential cannot end up in a URL that a client, proxy or
exception string might echo.
"""

import os
from typing import Any, Optional

import httpx

from .security import scrub

DEFAULT_BASE_URL = "https://api.evomi.com"


class EvomiPublicAPIError(RuntimeError):
    """An Evomi Public API call failed. The message never contains credentials."""


class EvomiAuthError(EvomiPublicAPIError):
    """The Public API key is missing, invalid, or not permitted for this call."""


class EvomiPublicClient:
    """HTTP client for the Evomi Public API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """
        Initialize the Public API client.

        Args:
            api_key: Evomi Public API key. Defaults to EVOMI_PUBLIC_API_KEY, then
                EVOMI_API_KEY.
            base_url: API base URL (defaults to EVOMI_PUBLIC_BASE_URL env var or
                https://api.evomi.com)
        """
        self.api_key = api_key or os.getenv("EVOMI_PUBLIC_API_KEY") or os.getenv("EVOMI_API_KEY")
        self.base_url = (
            base_url or os.getenv("EVOMI_PUBLIC_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")

        if not self.api_key:
            raise EvomiAuthError(
                "No Evomi Public API key configured. Set EVOMI_PUBLIC_API_KEY to the "
                "key from https://my.evomi.com/settings/api (personal) or Settings > "
                "Team (team key). EVOMI_API_KEY is used as a fallback."
            )

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        return {
            "x-apikey": self.api_key,
            "Accept": "application/json",
        }

    async def _get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
        as_text: bool = False,
    ) -> Any:
        """Perform a GET against the Public API. See _request for failure modes."""
        return await self._request(
            "GET", path, params=params, timeout=timeout, as_text=as_text
        )

    async def _post(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> Any:
        """
        Perform a POST against the Public API. See _request for failure modes.

        Parameters travel in the query string, which is how the rest of /public
        takes them. The credential is still header-only.
        """
        return await self._request("POST", path, params=params, timeout=timeout)

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
        as_text: bool = False,
    ) -> Any:
        """
        Send one request to the Public API and normalise its failure modes.

        Raises EvomiPublicAPIError with a message safe to show a user: it carries
        the status and, where the API supplied one, its own `error` string —
        never the request URL, headers, or a raw body.
        """
        url = f"{self.base_url}/public{path}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method, url, params=params, headers=self._get_headers()
                )
        except httpx.HTTPError as exc:
            raise EvomiPublicAPIError(
                f"Could not reach the Evomi Public API ({type(exc).__name__})."
            ) from None

        if response.status_code in (401, 403):
            raise EvomiAuthError(
                f"Evomi Public API rejected the API key (HTTP {response.status_code}). "
                "Check that EVOMI_PUBLIC_API_KEY holds a current key from "
                "https://my.evomi.com/settings/api, and that the account's email is verified."
            )

        if response.status_code >= 400:
            raise EvomiPublicAPIError(
                f"Evomi Public API returned HTTP {response.status_code}"
                f"{self._api_error_detail(response)}"
            )

        if as_text:
            return response.text

        try:
            return response.json()
        except ValueError:
            raise EvomiPublicAPIError(
                f"Evomi Public API returned a non-JSON response (HTTP {response.status_code})."
            ) from None

    @staticmethod
    def _api_error_detail(response: httpx.Response) -> str:
        """Extract the API's own `error` string, if it sent one, scrubbed."""
        try:
            payload = response.json()
        except ValueError:
            return "."

        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return f": {scrub(payload['error'])}"

        return "."

    # ─── Proxy Products & Credentials ───────────────────────────────────────────

    async def get_proxy_products(self) -> dict[str, Any]:
        """
        GET /public — proxy credentials, balances and endpoints for every product.

        Returns the raw payload, which contains plaintext proxy passwords. Callers
        are responsible for masking before anything reaches the model.
        """
        return await self._get("")

    async def generate_proxies(self, **params: Any) -> str:
        """
        GET /public/generate — server-side proxy string generation.

        Returns plain text, one proxy per line. Values are passed through as
        given; the endpoint validates them and reports its own errors.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._get("/generate", params=clean, as_text=True)

    # ─── Usage & Targeting ──────────────────────────────────────────────────────

    async def get_usage(self, product: str, period: str = "3d") -> dict[str, Any]:
        """GET /public/usage — bandwidth and request statistics for one product."""
        return await self._get("/usage", params={"product": product, "period": period})

    async def get_settings(self) -> dict[str, Any]:
        """GET /public/settings — available geo/ISP targeting values per product."""
        return await self._get("/settings", timeout=60.0)

    # ─── Sessions ───────────────────────────────────────────────────────────────

    async def rotate_session(self, session_id: str, product: str) -> dict[str, Any]:
        """GET /public/rotate_session — force a new exit IP for a sticky session."""
        return await self._get(
            "/rotate_session", params={"sessionid": session_id, "product": product}
        )

    # ─── Other Product Access ───────────────────────────────────────────────────

    async def get_scraper_access(self) -> dict[str, Any]:
        """GET /public/scraper — scraper credits, concurrency and API key."""
        return await self._get("/scraper")

    async def get_browser_access(self) -> dict[str, Any]:
        """GET /public/browser — browser credits, concurrency and API key."""
        return await self._get("/browser")

    async def list_browser_profiles(self) -> dict[str, Any]:
        """GET /public/profiles — browser fingerprint profiles owned by the account."""
        return await self._get("/profiles")

    async def order_browser_profile(
        self, os_name: str, browser_version: str
    ) -> dict[str, Any]:
        """
        POST /public/order-profile — create a browser fingerprint profile.

        Billable: past the first three profiles on an account, each one deducts
        1.5 GB from the proxy data balance. Values are passed through as given;
        the endpoint validates them and reports its own errors.
        """
        return await self._post(
            "/order-profile", params={"os": os_name, "browserVersion": browser_version}
        )
