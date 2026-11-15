"""Adapter registry.

Adapters are referenced by dotted path and imported on demand so that a
broken or half-finished provider module cannot stop the application from
starting - it is reported as unavailable on the diagnostics screen instead.
"""

from __future__ import annotations

import importlib
import logging
from functools import lru_cache

from app.config import get_settings
from app.providers.base import ProviderAdapter, ProviderCapabilities

log = logging.getLogger(__name__)

#: provider key -> "module path:class name".
ADAPTER_PATHS: dict[str, str] = {
    "mock": "app.providers.mock:MockProvider",
    "manual": "app.providers.manual:ManualAssistProvider",
    "alibaba": "app.providers.alibaba:AlibabaModelStudioProvider",
    "byteplus": "app.providers.byteplus:BytePlusSeedanceProvider",
    "fal": "app.providers.fal:FalProvider",
    "replicate": "app.providers.replicate:ReplicateProvider",
    "minimax": "app.providers.minimax:MiniMaxProvider",
    "pixverse": "app.providers.pixverse:PixVerseProvider",
    "runway": "app.providers.runway:RunwayProvider",
    "luma": "app.providers.luma:LumaProvider",
    "pika": "app.providers.pika:PikaProvider",
    "vidu": "app.providers.vidu:ViduProvider",
    "kling": "app.providers.kling:KlingProvider",
}

#: Populated when an adapter module fails to import, for the health screen.
IMPORT_ERRORS: dict[str, str] = {}

_INSTANCES: dict[str, ProviderAdapter] = {}


def _load_class(key: str) -> type[ProviderAdapter] | None:
    path = ADAPTER_PATHS.get(key)
    if path is None:
        return None
    module_name, _, class_name = path.partition(":")
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        IMPORT_ERRORS[key] = f"{type(exc).__name__}: {exc}"
        log.error("Adapter failed to import", extra={"provider": key, "error": str(exc)})
        return None


def get_adapter(key: str) -> ProviderAdapter | None:
    """Return a shared adapter instance wired to the configured credentials."""
    if key in _INSTANCES:
        return _INSTANCES[key]
    cls = _load_class(key)
    if cls is None:
        return None
    credentials = get_settings().credential_for(key)
    try:
        instance = cls(credentials)
    except Exception as exc:  # noqa: BLE001
        IMPORT_ERRORS[key] = f"{type(exc).__name__}: {exc}"
        log.error("Adapter failed to construct", extra={"provider": key, "error": str(exc)})
        return None
    _INSTANCES[key] = instance
    return instance


def available_keys() -> list[str]:
    return list(ADAPTER_PATHS)


@lru_cache(maxsize=None)
def get_capabilities(key: str) -> ProviderCapabilities | None:
    adapter = get_adapter(key)
    if adapter is None:
        return None
    try:
        return adapter.capabilities()
    except Exception as exc:  # noqa: BLE001
        IMPORT_ERRORS[key] = f"capabilities(): {type(exc).__name__}: {exc}"
        return None


def all_capabilities() -> dict[str, ProviderCapabilities]:
    result: dict[str, ProviderCapabilities] = {}
    for key in ADAPTER_PATHS:
        caps = get_capabilities(key)
        if caps is not None:
            result[key] = caps
    return result


def reset_cache() -> None:
    """Drop cached instances; used by tests and after a credential change."""
    _INSTANCES.clear()
    IMPORT_ERRORS.clear()
    get_capabilities.cache_clear()


def install(key: str, adapter: ProviderAdapter) -> None:
    """Inject a ready-made adapter instance (used by tests)."""
    _INSTANCES[key] = adapter
    get_capabilities.cache_clear()


def register(key: str, dotted_path: str) -> None:
    """Register an adapter at runtime (used by tests)."""
    ADAPTER_PATHS[key] = dotted_path
    reset_cache()


async def close_all() -> None:
    for adapter in list(_INSTANCES.values()):
        try:
            await adapter.aclose()
        except Exception:  # noqa: BLE001
            log.warning("Adapter close failed", extra={"provider": adapter.key})
    _INSTANCES.clear()
