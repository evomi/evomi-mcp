"""Helpers for keeping credentials out of tool output and error messages."""

import os
import re
from typing import Any, Iterable

SECRET_ENV_VARS = (
    "EVOMI_API_KEY",
    "EVOMI_PUBLIC_API_KEY",
    "EVOMI_SCRAPER_API_KEY",
)

MASK = "•" * 8

_QUERY_KEY_PATTERN = re.compile(r"(apikey|api_key|x-apikey|x-api-key)=([^&\s\"']+)", re.IGNORECASE)


def mask_secret(value: Any) -> str | None:
    """
    Render a secret as a fixed mask that carries none of its characters.

    Returns None for empty input, so a caller can distinguish "absent" from
    "present but hidden".
    """
    if not value:
        return None

    return MASK


def scrub(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """
    Remove credentials from a string that is about to be returned to the client.

    Covers three routes a key can take into an error message: the configured
    env vars, anything the caller knows is sensitive (a proxy password read
    from an API response, or a key fetched at runtime), and `apikey=` style
    query parameters that an HTTP library may have echoed back inside a URL.
    """
    result = text

    secrets = [os.getenv(name) for name in SECRET_ENV_VARS]
    secrets.extend(extra_secrets)

    for secret in secrets:
        if secret and len(str(secret)) >= 8:
            result = result.replace(str(secret), "[redacted]")

    return _QUERY_KEY_PATTERN.sub(r"\1=[redacted]", result)
