"""Shared fixtures. No test in this suite makes a network call."""

import pytest

from evomi_mcp import proxy_tools


class StubPublicClient:
    """Stands in for EvomiPublicClient, recording what it was asked for."""

    def __init__(self, products=None, **responses):
        self._products = products or {}
        self._responses = responses
        self.calls = []

    async def get_proxy_products(self):
        self.calls.append(("get_proxy_products", {}))
        return self._products

    async def generate_proxies(self, **params):
        self.calls.append(("generate_proxies", params))
        return self._responses.get("generate", "")

    async def get_usage(self, product, period="3d"):
        self.calls.append(("get_usage", {"product": product, "period": period}))
        return self._responses.get("usage", {})

    async def get_settings(self):
        self.calls.append(("get_settings", {}))
        return self._responses.get("settings", {})

    async def rotate_session(self, session_id, product):
        self.calls.append(("rotate_session", {"session_id": session_id, "product": product}))
        return self._responses.get("rotate", {"success": True, "message": "Session reset successfully"})

    async def get_scraper_access(self):
        self.calls.append(("get_scraper_access", {}))
        return self._responses.get("scraper", {})

    async def get_browser_access(self):
        self.calls.append(("get_browser_access", {}))
        return self._responses.get("browser", {})

    async def list_browser_profiles(self):
        self.calls.append(("list_browser_profiles", {}))
        return self._respond("profiles", {"profiles": []})

    async def order_browser_profile(self, os_name, browser_version):
        self.calls.append(
            ("order_browser_profile", {"os": os_name, "browser_version": browser_version})
        )
        return self._respond("order", {"success": True, "profile": {"id": "prof_1"}})

    def _respond(self, key, default):
        """A configured Exception is raised, so failure paths can be exercised."""
        result = self._responses.get(key, default)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def stub(monkeypatch):
    def install(products=None, **responses):
        client = StubPublicClient(products, **responses)
        monkeypatch.setattr(proxy_tools, "get_public_client", lambda: client)
        return client

    return install


@pytest.fixture(autouse=True)
def public_api_key(monkeypatch):
    """Give the Public API client a key so construction succeeds offline."""
    monkeypatch.setenv("EVOMI_PUBLIC_API_KEY", "test-public-key-0000")
    monkeypatch.delenv("EVOMI_HIDE_PROXY_PASSWORDS", raising=False)
    monkeypatch.delenv("EVOMI_ENABLE_SPENDING", raising=False)


@pytest.fixture
def products_payload():
    """A GET /public response shaped like the one documented at docs.evomi.com."""
    return {
        "success": True,
        "products": {
            "rp": {
                "username": "acct_user",
                "password": "rp_secret_pw",
                "balance_mb": 5120.5,
                "endpoint": "premium-residential.evomi.com",
                "ports": {"http": 1000, "socks5": 1002},
            },
            "rpc": {
                "username": "acct_user_core",
                "password": "rpc_secret_pw",
                "balance_mb": 20480.5,
                "endpoint": "core-residential.evomi.com",
                "ports": {"http": 1000, "socks5": 1002},
            },
            "mp": {
                "username": "acct_user",
                "password": "mp_secret_pw",
                "balance_mb": 0,
                "endpoint": "mp.evomi.com",
                "ports": {"http": 3000, "socks5": 3002},
            },
            "dcp": {
                "username": "acct_user",
                "password": "dcp_secret_pw",
                "balance_mb": 2048,
                "endpoint": "dcp.evomi.com",
                "ports": {"http": 2000, "socks5": 2002},
            },
            "static_residential": {
                "packages": [
                    {
                        "expiryDate": "2026-12-01T00:00:00.000Z",
                        "ips": [
                            {
                                "password": "static_pw_1",
                                "ipInfo": {"ip": "203.0.113.10"},
                            },
                            {
                                "password": "static_pw_2",
                                "ipInfo": {"ip": "203.0.113.11"},
                            },
                        ],
                    }
                ],
                "ports": {"http": 12345, "socks5": 12346},
            },
        },
    }
