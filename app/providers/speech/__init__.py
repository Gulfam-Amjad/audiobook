"""Speech engine registry.

Mirrors :mod:`app.providers.script`. There is exactly one engine today,
and the registry exists so adding a second (a remote TTS Space, say) does
not mean touching the services that call it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.providers.speech.base import SpeechProvider

log = logging.getLogger(__name__)

PROVIDER_KEYS: tuple[str, ...] = ("piper", "kokoro", "edge")
DEFAULT_KEY = "piper"

IMPORT_ERRORS: dict[str, str] = {}

_CACHE: dict[str, SpeechProvider] = {}


def _construct(key: str) -> SpeechProvider | None:
    try:
        if key == "piper":
            from app.providers.speech.piper import PiperSpeechProvider

            return PiperSpeechProvider()
        if key == "kokoro":
            from app.providers.speech.kokoro import KokoroSpeechProvider

            return KokoroSpeechProvider()
        if key == "edge":
            from app.providers.speech.edge import EdgeSpeechProvider

            return EdgeSpeechProvider()
    except Exception as exc:  # noqa: BLE001 - surfaced on the diagnostics screen
        IMPORT_ERRORS[key] = str(exc)
        log.error("Speech provider %s failed to load", key, exc_info=True)
        return None
    return None


def get(key: str = DEFAULT_KEY) -> SpeechProvider | None:
    if key not in _CACHE:
        provider = _construct(key)
        if provider is None:
            return None
        _CACHE[key] = provider
    return _CACHE[key]


def all_providers() -> list[SpeechProvider]:
    return [p for p in (get(key) for key in PROVIDER_KEYS) if p is not None]


def describe() -> list[dict[str, Any]]:
    """Rows for the UI. Availability is checked separately; this is static."""
    rows: list[dict[str, Any]] = []
    for provider in all_providers():
        caps = provider.capabilities()
        rows.append(
            {
                "key": caps.key,
                "display_name": caps.display_name,
                "local": caps.local,
                "licence": caps.licence,
                "licence_note": caps.licence_note,
                "docs_url": caps.docs_url,
                "checked_on": caps.checked_on,
                "notes": caps.notes,
            }
        )
    return rows


def reset_cache() -> None:
    _CACHE.clear()
    IMPORT_ERRORS.clear()
