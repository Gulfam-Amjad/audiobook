"""Vidu (Shengshu) image-to-video.

Sources, all read 2026-10-28:

* Docs index - https://platform.vidu.com/docs/llms.txt
* Pricing - https://platform.vidu.com/docs/pricing.md
* Usage and limits - https://platform.vidu.com/docs/terms-and-policies/usage-and-limits.md

Vidu publishes prices in credits and RMB only. The USD figures below are
derived at an assumed rate and are therefore marked ``estimated``, never
``verified`` - the credit counts are the verified part.
"""

from __future__ import annotations

from app.constants import ErrorClass, ProviderKind
from app.providers.base import (
    GenerationRequest,
    ImageConstraints,
    ModelSpec,
    PriceSpec,
    ProviderCapabilities,
    SubmitResult,
    TaskStatus,
)
from app.providers.http_adapter import HttpProviderAdapter, data_uri

#: Verified: 1 credit = CNY 0.03125 (platform.vidu.com/docs/pricing.md).
CNY_PER_CREDIT = 0.03125
#: Assumed, not verified. Used only to turn credits into an indicative USD
#: figure; the Providers screen states the assumption.
ASSUMED_USD_PER_CNY = 0.14

#: 10 MB once base64 is decoded, per the input limits.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PRICE_SOURCE = "https://platform.vidu.com/docs/pricing.md"


def _price(credits_per_second: dict[str, float], note: str) -> PriceSpec:
    usd = {
        resolution: round(value * CNY_PER_CREDIT * ASSUMED_USD_PER_CNY, 6)
        for resolution, value in credits_per_second.items()
    }
    return PriceSpec(
        mode="per_second",
        usd_per_second=usd,
        units_per_second=dict(credits_per_second),
        unit_name="credits",
        usd_per_unit=round(CNY_PER_CREDIT * ASSUMED_USD_PER_CNY, 6),
        # The credit counts are verified; the dollar conversion is not.
        confidence="estimated",
        source_url=_PRICE_SOURCE,
        checked_on="2026-10-28",
        notes=(
            f"{note} Credit rates are from Vidu's own table; USD is converted at an "
            f"assumed CNY {CNY_PER_CREDIT:.5f} per credit and "
            f"{ASSUMED_USD_PER_CNY} USD per CNY, so treat the dollar figure as "
            "indicative only."
        ),
    )


class ViduProvider(HttpProviderAdapter):
    key = "vidu"
    kind = ProviderKind.API
    base_url = "https://api.vidu.com"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            key=self.key,
            display_name="Vidu",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="viduq3-turbo",
                    label="Vidu Q3 Turbo",
                    durations=(5,),
                    resolutions=("540p", "720p", "1080p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_seed=True,
                    price=_price(
                        {"540p": 7.0, "720p": 12.0, "1080p": 13.0},
                        "Per second of output.",
                    ),
                    extra_settings={
                        "movement_amplitude": {
                            "label": "Movement amplitude",
                            "type": "choice",
                            "choices": ["auto", "small", "medium", "large"],
                        }
                    },
                ),
                ModelSpec(
                    id="viduq3-pro",
                    label="Vidu Q3 Pro",
                    durations=(5,),
                    resolutions=("540p", "720p", "1080p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_seed=True,
                    price=_price(
                        {"540p": 9.0, "720p": 20.0, "1080p": 24.0},
                        "Per second of output.",
                    ),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=MAX_IMAGE_BYTES,
                min_aspect=0.25,
                max_aspect=4.0,
                accepts_public_url=True,
                accepts_base64=True,
                notes="Exactly one image. 50 MB by URL, 10 MB once base64 is decoded.",
            ),
            required_credentials=("api_key",),
            default_concurrency=5,
            default_requests_per_minute=None,
            min_poll_interval_s=10,
            result_url_ttl_hours=24,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://platform.vidu.com/docs/llms.txt",
            signup_url="https://platform.vidu.com",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "No free API credits are published. The consumer site at vidu.com does "
                "give a signup bonus and daily-login credits, but the amounts are "
                "unpublished and there is no evidence they can be spent through the API. "
                "Treat website credits as manual-assist only (checked 2026-10-28)."
            ),
            automation_note=(
                "The API terms ban \"cheating, hacking, botting\" and reverse "
                "engineering, and forbid proxies or VPNs used to bypass regional "
                "restrictions. Use the documented API, not the website."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Billing is RMB-denominated. The USD prices shown here are converted at "
                "an assumed exchange rate and are indicative only.",
                "Five concurrent tasks per organisation; extra tasks queue rather than "
                "being rejected.",
                "Off-peak generation (delivered within 48 hours) is roughly half price, "
                "which this adapter does not request.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        payload: dict[str, object] = {
            "model": request.model_id,
            "images": [data_uri(request.image_path, max_bytes=MAX_IMAGE_BYTES)],
            "prompt": request.prompt,
            "duration": request.duration_s,
            "resolution": request.resolution,
            "aspect_ratio": request.aspect_ratio,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        amplitude = request.extra.get("movement_amplitude")
        if amplitude:
            payload["movement_amplitude"] = amplitude

        data = await self.request_json(
            "POST", "/ent/v2/img2video", json_body=payload, uncertain_on_timeout=True
        )
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise self.unexpected(data, "submit response without a task_id")
        return SubmitResult(task_ref=str(task_id), accepted=True, message="Queued by Vidu.")

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/ent/v2/tasks/{task_ref}/creations")
        state = str(data.get("state") or data.get("status") or "").lower()

        if state in ("success", "succeeded", "completed"):
            creations = data.get("creations") or []
            url = None
            if isinstance(creations, list) and creations:
                first = creations[0]
                url = first.get("url") if isinstance(first, dict) else first
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Vidu reported success but returned no creation URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if state in ("failed", "fail"):
            reason = str(data.get("err_code") or data.get("message") or "no reason given")
            lowered = reason.lower()
            error_class = (
                ErrorClass.MODERATION
                if any(word in lowered for word in ("sensitive", "moderation", "policy"))
                else ErrorClass.TRANSIENT
            )
            return TaskStatus(state="failed", message=reason, error_class=error_class, raw=data)

        if state in ("created", "queueing", "pending"):
            return TaskStatus(state="pending", message="Queued by Vidu.", raw=data)
        if state in ("processing", "running"):
            return TaskStatus(state="running", message="Generating.", raw=data)
        return TaskStatus(state="unknown", message=f"Unrecognised state '{state}'.", raw=data)
