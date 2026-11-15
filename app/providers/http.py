"""Shared HTTP client for provider adapters.

Two deliberate choices:

* **IPv4 is forced** by binding the local address to ``0.0.0.0``. On this
  machine IPv6 routes to Fastly-hosted endpoints (PyPI, several provider
  CDNs) accept the connection and then hang, which shows up as mysterious
  20-second stalls mid-poll. Binding an IPv4 source address makes the
  resolver skip AAAA results entirely.

* **Errors are classified, not just raised.** :func:`classify_http_error`
  turns a status code into an :class:`ErrorClass` so the scheduler knows
  whether retrying is safe, pointless, or actively dangerous.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.constants import ErrorClass
from app.logging_setup import redact
from app.providers.base import ProviderError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
#: Downloads of generated video need a longer read window.
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)


def build_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(
        retries=0,  # the scheduler owns retry policy, not the transport
        local_address="0.0.0.0",  # force IPv4; see module docstring
    )
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers or {},
        timeout=timeout or DEFAULT_TIMEOUT,
        transport=transport,
        follow_redirects=True,
    )


def classify_http_error(status_code: int, body: str = "") -> ErrorClass:
    """Map an HTTP status onto a retry policy."""
    lowered = body.lower()
    if status_code in (401, 403):
        # Some providers use 403 for moderation refusals rather than auth.
        if any(word in lowered for word in ("moderation", "sensitive", "policy", "nsfw")):
            return ErrorClass.MODERATION
        return ErrorClass.AUTH
    if status_code == 402:
        return ErrorClass.INSUFFICIENT_CREDIT
    if status_code == 429:
        return ErrorClass.RATE_LIMITED
    if status_code in (400, 404, 413, 415, 422):
        if any(word in lowered for word in ("credit", "balance", "quota", "insufficient")):
            return ErrorClass.INSUFFICIENT_CREDIT
        if any(word in lowered for word in ("moderation", "sensitive", "policy", "nsfw")):
            return ErrorClass.MODERATION
        return ErrorClass.INVALID_INPUT
    if 500 <= status_code < 600:
        return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN


def sanitize_provider_message(text: str, limit: int = 400) -> str:
    """Trim, flatten and redact a provider error body for safe display.

    Provider responses are untrusted input that ends up in HTML, in the
    database and on screen, so control characters are stripped and the text
    is truncated. Credentials are redacted here rather than relying on the
    logging formatter, because most of these strings are shown to the user
    and stored long before any log handler sees them - providers do echo the
    offending authorization header back in an error body.
    """
    if not text:
        return ""
    flat = " ".join(text.split())
    cleaned = "".join(ch for ch in flat if ch.isprintable())
    cleaned = redact(cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "\u2026"
    return cleaned


def raise_for_status(response: httpx.Response, provider: str) -> None:
    """Convert a non-2xx response into a classified :class:`ProviderError`."""
    if response.is_success:
        return
    body = sanitize_provider_message(response.text)
    error_class = classify_http_error(response.status_code, body)
    retry_after = _retry_after_seconds(response)
    raise ProviderError(
        f"{provider} returned HTTP {response.status_code}: {body or 'no body'}",
        error_class,
        retry_after_s=retry_after,
        raw={"status_code": response.status_code},
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def wrap_transport_error(exc: Exception, provider: str) -> ProviderError:
    """Translate an httpx transport failure into a classified error.

    A timeout during *submit* is the dangerous case: the provider may well
    have accepted the job and started charging, so it is classified as
    ``UNCERTAIN`` rather than ``TRANSIENT``. The caller decides which applies
    by catching this and re-raising with the right class if it knows better.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(
            f"{provider} timed out: {type(exc).__name__}", ErrorClass.TRANSIENT
        )
    if isinstance(exc, httpx.TransportError):
        return ProviderError(
            f"Could not reach {provider}: {sanitize_provider_message(str(exc))}",
            ErrorClass.TRANSIENT,
        )
    return ProviderError(
        f"{provider} request failed: {sanitize_provider_message(str(exc))}",
        ErrorClass.UNKNOWN,
    )


def json_or_error(response: httpx.Response, provider: str) -> dict[str, Any]:
    raise_for_status(response, provider)
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"{provider} returned a non-JSON body.", ErrorClass.TRANSIENT
        ) from exc
    if not isinstance(data, dict):
        return {"data": data}
    return data
