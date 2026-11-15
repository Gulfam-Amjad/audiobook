"""Borrowed-GPU backend registry.

Order is the preference order: Kaggle first because it runs code we
control on a known allowance, a Hugging Face Space second because it can
only call whatever app the user points it at.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.constants import RemotePurpose
from app.providers.compute.base import ComputeProvider

log = logging.getLogger(__name__)

PROVIDER_KEYS: tuple[str, ...] = ("kaggle", "hf_space")

IMPORT_ERRORS: dict[str, str] = {}

_CACHE: dict[str, ComputeProvider] = {}


def _credentials(key: str, purpose: str = RemotePurpose.IMAGE_TO_VIDEO) -> dict[str, str]:
    settings = get_settings()
    if key == "kaggle":
        values = {"username": settings.kaggle_username, "key": settings.kaggle_key}
    elif key == "hf_space":
        space = (
            settings.hf_space_lipsync
            if purpose == RemotePurpose.LIPSYNC
            else settings.hf_space_i2v
        )
        values = {
            "space": space,
            "token": settings.hf_token,
            "api_name": settings.hf_space_api_name,
        }
    else:
        values = {}
    return {k: v for k, v in values.items() if v}


def _construct(key: str, purpose: str) -> ComputeProvider | None:
    try:
        if key == "kaggle":
            from app.providers.compute.kaggle import KaggleCompute

            return KaggleCompute(_credentials(key, purpose))
        if key == "hf_space":
            from app.providers.compute.hf_space import HuggingFaceSpaceCompute

            return HuggingFaceSpaceCompute(_credentials(key, purpose))
    except Exception as exc:  # noqa: BLE001 - surfaced on the diagnostics screen
        IMPORT_ERRORS[key] = str(exc)
        log.error("Compute provider %s failed to load", key, exc_info=True)
        return None
    return None


def get(
    key: str, purpose: str = RemotePurpose.IMAGE_TO_VIDEO
) -> ComputeProvider | None:
    # A Space depends on the purpose, so the cache key has to as well.
    cache_key = f"{key}:{purpose}"
    if cache_key not in _CACHE:
        provider = _construct(key, purpose)
        if provider is None:
            return None
        _CACHE[cache_key] = provider
    return _CACHE[cache_key]


def all_providers(
    purpose: str = RemotePurpose.IMAGE_TO_VIDEO,
) -> list[ComputeProvider]:
    return [p for p in (get(key, purpose) for key in PROVIDER_KEYS) if p is not None]


def preferred(purpose: str) -> ComputeProvider | None:
    """The first configured backend that supports this kind of work.

    Returns None when none is configured, which the caller must report as
    blocked rather than working around.
    """
    for key in PROVIDER_KEYS:
        provider = get(key, purpose)
        if provider is None:
            continue
        caps = provider.capabilities()
        if purpose in caps.purposes and provider.is_configured:
            return provider
    return None


def describe(purpose: str = RemotePurpose.IMAGE_TO_VIDEO) -> list[dict[str, Any]]:
    """Rows for the UI. No call is made, so nothing here is 'live verified'."""
    rows: list[dict[str, Any]] = []
    for provider in all_providers(purpose):
        caps = provider.capabilities()
        rows.append(
            {
                "key": caps.key,
                "display_name": caps.display_name,
                "configured": provider.is_configured,
                "purposes": caps.purposes,
                "accelerators": caps.accelerators,
                "default_accelerator": caps.default_accelerator,
                "free_tier_note": caps.free_tier_note,
                "docs_url": caps.docs_url,
                "signup_url": caps.signup_url,
                "checked_on": caps.checked_on,
                "endpoint_verified": caps.endpoint_verified,
                "live_verified": caps.live_verified,
                "notes": caps.notes,
                "missing": [
                    name
                    for name in caps.required_credentials
                    if not provider.credentials.get(name)
                ],
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
