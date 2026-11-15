"""Shared plumbing for adapters that talk to an HTTP API.

Each concrete adapter supplies a base URL, auth headers and the three calls
that differ between providers (submit, poll, and sometimes a balance read).
Everything repetitive - client lifetime, error classification, JSON decoding,
and turning a local image into whatever payload the provider accepts - lives
here so eleven adapters do not repeat it eleven times.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from app.constants import ErrorClass
from app.providers.base import AuthResult, ProviderAdapter, ProviderError
from app.providers.http import (
    build_client,
    json_or_error,
    raise_for_status,
    sanitize_provider_message,
    wrap_transport_error,
)

log = logging.getLogger(__name__)


def read_image(path: str | Path, *, max_bytes: int | None = None) -> tuple[bytes, str]:
    """Load a source frame, refusing anything the provider would reject.

    Returns ``(data, mime_type)``. Raises a classified
    :class:`ProviderError` rather than an ``OSError`` so the scheduler can
    tell "this file is wrong" from "the network is down".
    """
    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise ProviderError(
            f"Could not read the source image: {exc.strerror or exc}",
            ErrorClass.INVALID_INPUT,
        ) from exc

    if max_bytes is not None and len(data) > max_bytes:
        raise ProviderError(
            f"The source image is {len(data) / 1_048_576:.1f} MB but this provider accepts "
            f"at most {max_bytes / 1_048_576:.0f} MB. Resize it and try again.",
            ErrorClass.INVALID_INPUT,
        )

    mime = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
    return data, mime


def encode_data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def data_uri(path: str | Path, *, max_bytes: int | None = None) -> str:
    """Encode a source frame as a ``data:`` URI.

    Note the 4/3 expansion: a provider that caps the *request body* is really
    capping ``max_bytes * 3 / 4`` of image, which each adapter accounts for
    in the limit it passes in.
    """
    data, mime = read_image(path, max_bytes=max_bytes)
    return encode_data_uri(data, mime)


class HttpProviderAdapter(ProviderAdapter):
    """Base class for REST-based providers."""

    #: Root URL for the provider's API, without a trailing slash.
    base_url: str = ""
    #: Path used by :meth:`check_auth` for a cheap authenticated GET. When
    #: empty, the adapter reports that it has no connection test.
    auth_probe_path: str = ""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._client: httpx.AsyncClient | None = None

    # -- client -----------------------------------------------------------

    def auth_headers(self) -> dict[str, str]:
        """Headers that carry the credential. Overridden per provider."""
        return {}

    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = build_client(
                base_url=self.base_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AIVideoManager/0.1 (local, single user)",
                    **self.auth_headers(),
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # -- requests -----------------------------------------------------------

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        files: Any | None = None,
        data: Any | None = None,
        uncertain_on_timeout: bool = False,
    ) -> dict[str, Any]:
        """Make one call and return the decoded JSON object.

        ``uncertain_on_timeout`` must be set for *submitting* calls. A submit
        that times out may already have created a billable task, and the
        scheduler treats :data:`ErrorClass.UNCERTAIN` as "never retry
        automatically".
        """
        name = self.capabilities().display_name
        try:
            response = await self.client().request(
                method,
                path,
                json=json_body,
                params=params,
                headers=headers,
                files=files,
                data=data,
            )
        except httpx.TimeoutException as exc:
            if uncertain_on_timeout:
                raise ProviderError(
                    f"{name} did not answer in time. It may have accepted the job anyway, "
                    "so this will not be retried automatically.",
                    ErrorClass.UNCERTAIN,
                ) from exc
            raise wrap_transport_error(exc, name) from exc
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, name) from exc

        return json_or_error(response, name)

    async def request_raw(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        name = self.capabilities().display_name
        try:
            response = await self.client().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise wrap_transport_error(exc, name) from exc
        raise_for_status(response, name)
        return response

    # -- defaults -----------------------------------------------------------

    async def check_auth(self) -> AuthResult:
        caps = self.capabilities()
        if not self.is_configured:
            missing = ", ".join(
                field for field in caps.required_credentials
                if not self.credentials.get(field)
            )
            return AuthResult(
                False, f"Missing credential(s): {missing}", reached_provider=False
            )

        if not self.auth_probe_path:
            return AuthResult(
                False,
                (
                    f"{caps.display_name} has no cheap call this adapter can use as a "
                    "connection test. Submit one real job to verify it."
                ),
                reached_provider=False,
            )

        try:
            await self.request_json("GET", self.auth_probe_path)
        except ProviderError as exc:
            if exc.error_class in (ErrorClass.AUTH, ErrorClass.INSUFFICIENT_CREDIT):
                return AuthResult(False, exc.message)
            if exc.error_class == ErrorClass.TRANSIENT:
                return AuthResult(
                    False,
                    f"Could not reach {caps.display_name}: {exc.message}",
                    reached_provider=False,
                )
            # Anything else means the request got through and was understood,
            # which is all a credential check needs to establish.
            return AuthResult(True, f"{caps.display_name} accepted the credentials.")

        return AuthResult(True, f"{caps.display_name} accepted the credentials.")

    def unexpected(self, payload: Any, what: str) -> ProviderError:
        """Error for a response that parsed but made no sense."""
        return ProviderError(
            f"{self.capabilities().display_name} returned an unexpected {what}: "
            f"{sanitize_provider_message(str(payload), 200)}",
            ErrorClass.UNKNOWN,
        )
