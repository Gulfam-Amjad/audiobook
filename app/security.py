"""Local access control.

The app binds to 127.0.0.1 by default, which already keeps it off the
network. The optional ``AVM_UI_TOKEN`` adds a second layer: any other
program running on this machine (including a web page in another tab making
cross-origin requests) cannot drive the API without it.

This is deliberately modest. A full auth system would be disproportionate
for a single-user local tool, but silently accepting every local request
from any origin would not be.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

from app.config import get_settings

COOKIE_NAME = "avm_ui_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _expected_token() -> str:
    return get_settings().ui_token.strip()


def token_required() -> bool:
    return bool(_expected_token())


def token_matches(candidate: str | None) -> bool:
    expected = _expected_token()
    if not expected:
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


async def require_local_access(request: Request) -> None:
    """FastAPI dependency guarding mutating endpoints.

    Rejects cross-origin writes outright so a malicious page cannot use the
    browser's ambient access to the loopback server, then checks the token
    when one is configured.
    """
    if request.method in _SAFE_METHODS:
        return

    settings = get_settings()
    allowed_hosts = {
        f"{settings.host}:{settings.port}",
        f"127.0.0.1:{settings.port}",
        f"localhost:{settings.port}",
    }

    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.netloc not in allowed_hosts:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-origin requests are not accepted.",
            )

    if not token_required():
        return

    supplied = request.headers.get("x-avm-token") or request.cookies.get(COOKIE_NAME)
    if not token_matches(supplied):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid UI token is required. Open the dashboard and unlock it.",
        )
