"""Script writer registry.

Mirrors :mod:`app.providers.registry`: adapters are constructed on demand
with whatever credentials the environment holds, and an import failure is
recorded rather than crashing the app.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.providers.script.base import ScriptProvider

log = logging.getLogger(__name__)

#: Order matters: the first configured, automated provider is the default.
PROVIDER_KEYS: tuple[str, ...] = ("gemini", "manual_paste")

IMPORT_ERRORS: dict[str, str] = {}

_CACHE: dict[str, ScriptProvider] = {}


def _credentials(key: str) -> dict[str, str]:
    settings = get_settings()
    mapping: dict[str, dict[str, str]] = {
        "gemini": {"api_key": settings.gemini_api_key},
        "manual_paste": {},
    }
    return {k: v for k, v in mapping.get(key, {}).items() if v}


def _construct(key: str) -> ScriptProvider | None:
    try:
        if key == "gemini":
            from app.providers.script.gemini import GeminiScriptProvider

            return GeminiScriptProvider(_credentials(key))
        if key == "manual_paste":
            from app.providers.script.manual_paste import ManualPasteScriptProvider

            return ManualPasteScriptProvider(_credentials(key))
    except Exception as exc:  # noqa: BLE001 - surfaced on the diagnostics screen
        IMPORT_ERRORS[key] = str(exc)
        log.error("Script provider %s failed to load", key, exc_info=True)
        return None
    return None


def get(key: str) -> ScriptProvider | None:
    if key not in _CACHE:
        provider = _construct(key)
        if provider is None:
            return None
        _CACHE[key] = provider
    return _CACHE[key]


def all_providers() -> list[ScriptProvider]:
    return [p for p in (get(key) for key in PROVIDER_KEYS) if p is not None]


def default_key() -> str:
    """The first automated provider that actually has its credentials.

    Falls back to the manual paste flow, which always works, so the script
    stage is never simply unavailable.
    """
    for key in PROVIDER_KEYS:
        provider = get(key)
        if provider is None:
            continue
        caps = provider.capabilities()
        if caps.automated and provider.is_configured:
            return key
    return "manual_paste"


def describe() -> list[dict[str, Any]]:
    """Rows for the UI. No call is made, so nothing here is 'verified'."""
    rows: list[dict[str, Any]] = []
    for provider in all_providers():
        caps = provider.capabilities()
        rows.append(
            {
                "key": caps.key,
                "display_name": caps.display_name,
                "automated": caps.automated,
                "configured": provider.is_configured,
                "models": caps.models,
                "default_model": caps.default_model,
                "free_tier_note": caps.free_tier_note,
                "docs_url": caps.docs_url,
                "signup_url": caps.signup_url,
                "checked_on": caps.checked_on,
                "notes": caps.integration_notes,
            }
        )
    return rows


def reset_cache() -> None:
    _CACHE.clear()
    IMPORT_ERRORS.clear()


async def close_all() -> None:
    for provider in list(_CACHE.values()):
        await provider.aclose()
    _CACHE.clear()
