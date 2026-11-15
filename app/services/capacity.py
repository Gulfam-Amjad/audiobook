"""What the free allowances can actually produce in a day.

Recurring website credits and one-off API trials are different wallets and
are never added into one daily number. A 20-minute montage made by repeating
stills is not 20 minutes of generated video, so assembled duration is kept
in its own field.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import AssemblyStatus
from app.models import Assembly, Output
from app.providers.manual_sites import SITES, total_seconds_per_day, total_signup_bonus_seconds
from app.services import settings_store

#: Alibaba's published trial, read 2026-10-28. 50 seconds per Wan
#: image-to-video model, Singapore scope, 90 days, once. Four models are
#: wired in the adapter, so the ceiling is 200 seconds, not a daily rate.
ALIBABA_TRIAL_SECONDS_PER_MODEL = 50
ALIBABA_TRIAL_MODELS = 4
ALIBABA_TRIAL_DAYS = 90

#: BytePlus publishes tokens, not seconds. Stating a clip count would require
#: a conversion this screen refuses to invent; the token grant is shown as tokens.
BYTEPLUS_TRIAL_TOKENS_PER_MODEL = 2_000_000
BYTEPLUS_TRIAL_MODELS = 2


@dataclass(frozen=True, slots=True)
class DailyBand:
    label: str
    seconds: float
    what_it_includes: str


def manual_bands() -> list[DailyBand]:
    """Three readings of the ten sites. Unverified figures stay in their own band."""
    verified = [site for site in SITES if site.allowance_verified]
    unverified = [site for site in SITES if not site.allowance_verified]
    low, high = total_seconds_per_day()
    verified_low = sum(site.seconds_per_day_low for site in verified)
    verified_high = sum(site.seconds_per_day_high for site in verified)
    return [
        DailyBand(
            "Conservative",
            verified_low,
            "Lowest published figure for each site whose allowance was confirmed "
            f"({len(verified)} of {len(SITES)}). Unverified reports are left out.",
        ),
        DailyBand(
            "Expected",
            (verified_low + verified_high) / 2,
            "Midpoint of the published ranges only. Still no unverified sites.",
        ),
        DailyBand(
            "Optimistic",
            high,
            "Highest figure for every site, including ones that were not confirmed "
            f"({', '.join(site.name for site in unverified) or 'none'}). "
            f"The confirmed-only ceiling is {verified_high:.0f}s. "
            f"The all-site floor is {low:.0f}s.",
        ),
    ]


async def generated_motion(session: AsyncSession) -> tuple[float, int, int]:
    """Real probed seconds, how many clips that covers, and how many were unmeasured."""
    measured = (
        await session.execute(
            select(func.coalesce(func.sum(Output.duration_s), 0.0), func.count(Output.id)).where(
                Output.duration_s.is_not(None)
            )
        )
    ).one()
    unmeasured = int(
        (
            await session.execute(
                select(func.count(Output.id)).where(Output.duration_s.is_(None))
            )
        ).scalar()
        or 0
    )
    return float(measured[0] or 0.0), int(measured[1] or 0), unmeasured


async def assembled_motion(session: AsyncSession) -> tuple[float, float]:
    """Finished assemblies: source-clip seconds, then the rendered file's seconds.

    The two differ when a transition overlaps clips. Neither is added to the
    generated-video total above; that total already counted the source clips.
    """
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Assembly.source_duration_s), 0.0),
                func.coalesce(func.sum(Assembly.output_duration_s), 0.0),
            ).where(Assembly.status == AssemblyStatus.COMPLETED)
        )
    ).one()
    return float(row[0] or 0.0), float(row[1] or 0.0)


async def report(session: AsyncSession) -> dict[str, object]:
    stored = await settings_store.get_all(session)
    target_s = float(stored["daily_target_minutes"]) * 60.0
    bands = manual_bands()
    optimistic = bands[-1].seconds
    generated_s, measured_clips, unmeasured = await generated_motion(session)
    source_s, rendered_s = await assembled_motion(session)
    shortfall = max(0.0, target_s - optimistic)
    return {
        "target_minutes": stored["daily_target_minutes"],
        "target_seconds": target_s,
        "bands": bands,
        "shortfall_seconds": shortfall,
        "reachable": shortfall <= 0,
        "signup_bonus_seconds": total_signup_bonus_seconds(),
        "sites": SITES,
        "alibaba_seconds_once": ALIBABA_TRIAL_SECONDS_PER_MODEL * ALIBABA_TRIAL_MODELS,
        "alibaba_days": ALIBABA_TRIAL_DAYS,
        "byteplus_tokens_once": BYTEPLUS_TRIAL_TOKENS_PER_MODEL * BYTEPLUS_TRIAL_MODELS,
        "generated_seconds": generated_s,
        "measured_clips": measured_clips,
        "unmeasured_clips": unmeasured,
        "assembled_source_seconds": source_s,
        "assembled_output_seconds": rendered_s,
    }
