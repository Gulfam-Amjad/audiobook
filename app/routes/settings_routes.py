"""Settings screen.

The spending section is the load-bearing part: paid generation is off and
the cap is $0 on a fresh install, and turning paid generation off again
withdraws every per-account authorisation so re-enabling it later cannot
silently re-arm spending on an account the user has forgotten about.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import accounts as account_service, generation, settings_store, spending

router = APIRouter()

PARTIAL = "partials/settings_form.html"

#: Guards against a typo in the cap field turning into a real bill. A higher
#: ceiling than this is almost certainly a mistake on a project whose whole
#: premise is free allowances.
MAX_MONTHLY_CAP_USD = 500.0


async def _context(session: AsyncSession) -> dict[str, object]:
    app_settings = await settings_store.get_all(session)
    paths = get_settings()
    return {
        "settings": app_settings,
        "spend": await spending.summary(session),
        "data_root": str(paths.data_root),
        "resolved_output_dir": str(
            (app_settings.get("output_dir_override") or "").strip() or paths.output_dir
        ),
        "resolutions": generation.KNOWN_RESOLUTIONS,
        "aspect_ratios": generation.KNOWN_ASPECT_RATIOS,
        "max_cap_usd": MAX_MONTHLY_CAP_USD,
    }


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "settings.html", await _context(session))


@router.post(
    "/settings/spending",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def save_spending(
    request: Request,
    paid_enabled: bool = Form(False),
    monthly_spend_cap_usd: float = Form(0.0),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if monthly_spend_cap_usd < 0:
        raise HTTPException(status_code=400, detail="A spending cap cannot be negative.")
    if monthly_spend_cap_usd > MAX_MONTHLY_CAP_USD:
        raise HTTPException(
            status_code=400,
            detail=(
                f"${monthly_spend_cap_usd:,.2f} is above this app's ${MAX_MONTHLY_CAP_USD:,.0f} "
                "ceiling. If you really mean to spend that much, do it through the "
                "provider's own console where the limits are theirs to enforce."
            ),
        )

    was_enabled = bool(await settings_store.get(session, "paid_enabled"))
    await settings_store.set_value(session, "paid_enabled", paid_enabled)
    await settings_store.set_value(session, "monthly_spend_cap_usd", monthly_spend_cap_usd)

    notes: list[str] = []
    if paid_enabled and not was_enabled:
        notes.append(
            "Paid generation is now on. Each account must still be authorised "
            "individually on the Credits screen before it can be charged."
        )
    elif was_enabled and not paid_enabled:
        revoked = await account_service.revoke_all_paid_authorisations(session)
        notes.append("Paid generation is off; nothing can be charged.")
        if revoked:
            notes.append(
                f"{revoked} account authorisation(s) were withdrawn, so turning this back "
                "on will not silently re-arm them."
            )
    else:
        notes.append("Spending settings saved.")

    if paid_enabled and monthly_spend_cap_usd <= 0:
        notes.append("The cap is $0, so every paid job will still be refused.")

    await session.commit()
    return fragment(request, PARTIAL, await _context(session), flash=" ".join(notes))


@router.post(
    "/settings/general",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def save_general(
    request: Request,
    default_duration_seconds: int = Form(5),
    default_resolution: str = Form("720p"),
    default_aspect_ratio: str = Form("16:9"),
    daily_target_minutes: float = Form(20.0),
    poll_interval_seconds: int = Form(10),
    max_attempts: int = Form(3),
    job_timeout_minutes: int = Form(30),
    organize_by_provider: bool = Form(False),
    organize_by_date: bool = Form(False),
    output_dir_override: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if not 1 <= default_duration_seconds <= 60:
        raise HTTPException(
            status_code=400, detail="Default clip length must be between 1 and 60 seconds."
        )
    if default_resolution not in generation.KNOWN_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown resolution '{default_resolution}'.")
    if default_aspect_ratio not in generation.KNOWN_ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail=f"Unknown ratio '{default_aspect_ratio}'.")
    if not 0 < daily_target_minutes <= 600:
        raise HTTPException(
            status_code=400, detail="The daily target must be between 0 and 600 minutes."
        )
    if not 3 <= poll_interval_seconds <= 300:
        raise HTTPException(
            status_code=400,
            detail="Polling faster than every 3 seconds risks being rate limited.",
        )
    if not 1 <= max_attempts <= 10:
        raise HTTPException(status_code=400, detail="Attempts must be between 1 and 10.")
    if not 1 <= job_timeout_minutes <= 240:
        raise HTTPException(
            status_code=400, detail="The job timeout must be between 1 and 240 minutes."
        )

    override = output_dir_override.strip()
    if override:
        from app.services.paths import validate_output_dir

        problem = validate_output_dir(override)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    await settings_store.set_many(
        session,
        {
            "default_duration_seconds": default_duration_seconds,
            "default_resolution": default_resolution,
            "default_aspect_ratio": default_aspect_ratio,
            "daily_target_minutes": daily_target_minutes,
            "poll_interval_seconds": poll_interval_seconds,
            "max_attempts": max_attempts,
            "job_timeout_minutes": job_timeout_minutes,
            "organize_by_provider": organize_by_provider,
            "organize_by_date": organize_by_date,
            "output_dir_override": override,
        },
    )
    await session.commit()
    return fragment(
        request,
        PARTIAL,
        await _context(session),
        flash="Settings saved. They apply to jobs queued from now on.",
    )
