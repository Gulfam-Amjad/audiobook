"""Runtime-editable settings, backed by the ``settings`` table.

Anything the user can change from the Settings screen lives here rather than
in the environment, so no restart is needed. Credentials deliberately do not:
they stay in ``.env`` where they are never served to a template.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting

#: Keys the Settings screen exposes, with their defaults.
DEFAULTS: dict[str, Any] = {
    # Spending
    "paid_enabled": False,
    "monthly_spend_cap_usd": 0.0,
    # Queue
    "queue_paused": True,
    "default_concurrency": 1,
    "poll_interval_seconds": 10,
    "max_attempts": 3,
    "submit_timeout_seconds": 120,
    "job_timeout_minutes": 30,
    "download_timeout_seconds": 300,
    # Generation defaults
    "default_duration_seconds": 5,
    "default_resolution": "720p",
    "default_aspect_ratio": "16:9",
    # Targets
    "daily_target_minutes": 20.0,
    # Paths (blank means "use the configured data root")
    "output_dir_override": "",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    # Output organisation
    "organize_by_provider": True,
    "organize_by_date": True,
}


async def get_all(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars().all()
    stored = {row.key: row.value.get("v") for row in rows if isinstance(row.value, dict)}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return merged


async def get(session: AsyncSession, key: str) -> Any:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    row = await session.get(Setting, key)
    if row is None or not isinstance(row.value, dict) or "v" not in row.value:
        return DEFAULTS[key]
    return row.value["v"]


async def set_value(session: AsyncSession, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    coerced = _coerce_to_default_type(key, value)
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value={"v": coerced}))
    else:
        row.value = {"v": coerced}
    await session.flush()


async def set_many(session: AsyncSession, values: dict[str, Any]) -> list[str]:
    """Apply several settings; returns the keys that were ignored."""
    ignored = []
    for key, value in values.items():
        if key in DEFAULTS:
            await set_value(session, key, value)
        else:
            ignored.append(key)
    return ignored


def _coerce_to_default_type(key: str, value: Any) -> Any:
    """Keep stored types consistent with the declared default."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return "" if value is None else str(value)
    return value
