"""Runway Gen-4 image-to-video.

Sources, all read 2026-10-28:

* API guide - https://docs.dev.runwayml.com/guides/using-the-api/
* Endpoint and ratios - https://docs.dev.runwayml.com/api.md
* Pricing (1 credit = $0.01) - https://docs.dev.runwayml.com/guides/pricing/
* Input limits - https://docs.dev.runwayml.com/assets/inputs/
* Output URL lifetime - https://docs.dev.runwayml.com/assets/outputs/
* Tiers and concurrency - https://docs.dev.runwayml.com/usage/tiers/

There is no free tier: credits must be purchased before the API answers at
all, and the documented minimum payment is $10.
"""

from __future__ import annotations

from app.constants import ErrorClass, ProviderKind
from app.providers.base import (
    GenerationRequest,
    ImageConstraints,
    ModelSpec,
    PriceSpec,
    ProviderCapabilities,
    ProviderError,
    SubmitResult,
    TaskStatus,
)
from app.providers.http_adapter import HttpProviderAdapter, data_uri

#: Runway encodes resolution and aspect ratio in one "width:height" string.
RATIOS: dict[str, str] = {
    "16:9": "1280:720",
    "9:16": "720:1280",
    "4:3": "1104:832",
    "3:4": "832:1104",
    "1:1": "960:960",
    "21:9": "1584:672",
}

#: 5 MB is the documented data-URI ceiling; base64 inflates by 4/3.
MAX_DATA_URI_IMAGE_BYTES = int(5 * 1024 * 1024 * 0.74)

_PRICE_SOURCE = "https://docs.dev.runwayml.com/guides/pricing/"


class RunwayProvider(HttpProviderAdapter):
    key = "runway"
    kind = ProviderKind.API
    base_url = "https://api.dev.runwayml.com"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "X-Runway-Version": "2024-11-06",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        ratios = tuple(RATIOS)
        durations = tuple(range(2, 11))
        return ProviderCapabilities(
            key=self.key,
            display_name="Runway",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="gen4_turbo",
                    label="Gen-4 Turbo",
                    durations=durations,
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    supports_seed=True,
                    price=PriceSpec(
                        mode="per_second",
                        usd_per_second={"720p": 0.05},
                        units_per_second={"720p": 5.0},
                        unit_name="credits",
                        usd_per_unit=0.01,
                        confidence="verified",
                        source_url=_PRICE_SOURCE,
                        checked_on="2026-10-28",
                        notes="5 credits per second of output, 1 credit = $0.01.",
                    ),
                ),
                ModelSpec(
                    id="gen4.5",
                    label="Gen-4.5",
                    durations=durations,
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    supports_seed=True,
                    price=PriceSpec(
                        mode="per_second",
                        usd_per_second={"720p": 0.12},
                        units_per_second={"720p": 12.0},
                        unit_name="credits",
                        usd_per_unit=0.01,
                        confidence="verified",
                        source_url=_PRICE_SOURCE,
                        checked_on="2026-10-28",
                        notes="12 credits per second of output.",
                    ),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=MAX_DATA_URI_IMAGE_BYTES,
                min_aspect=0.5,
                max_aspect=2.0,
                accepts_public_url=True,
                accepts_base64=True,
                notes=(
                    "Runway's own limits are 16 MB for a URL and 5 MB for a data URI. "
                    "This adapter always sends a data URI, and base64 inflates a file by "
                    "a third, so the usable limit on disk is the 3.7 MB shown. Images are "
                    "centre-cropped to the chosen ratio."
                ),
            ),
            required_credentials=("api_key",),
            default_concurrency=1,
            default_requests_per_minute=None,
            min_poll_interval_s=5,
            result_url_ttl_hours=24,
            supports_webhook=False,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://docs.dev.runwayml.com/guides/using-the-api/",
            signup_url="https://dev.runwayml.com",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "No free tier. Credits must be bought before the API will run anything, "
                "and the documented minimum payment is $10 (checked 2026-10-28)."
            ),
            automation_note=(
                "Using the documented API is the sanctioned route. Runway's consumer "
                "terms 1.3(e) prohibit automating the website instead."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Tier 1 allows one video task at a time, 50 generations a day and $100 a "
                "month; Tier 2 unlocks after $50 of purchases.",
                "Runway does not document an idempotency key, so a submit whose response "
                "is lost cannot be reconciled and will be parked for you to check by hand.",
            ),
        )

    def _ratio(self, request: GenerationRequest) -> str:
        ratio = RATIOS.get(request.aspect_ratio)
        if ratio is None:
            raise ProviderError(
                f"Runway has no output size for aspect ratio '{request.aspect_ratio}'.",
                ErrorClass.INVALID_INPUT,
            )
        return ratio

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        payload = {
            "model": request.model_id,
            "promptImage": data_uri(request.image_path, max_bytes=MAX_DATA_URI_IMAGE_BYTES),
            "promptText": request.prompt,
            "ratio": self._ratio(request),
            "duration": request.duration_s,
        }
        if request.seed is not None:
            payload["seed"] = request.seed

        data = await self.request_json(
            "POST", "/v1/image_to_video", json_body=payload, uncertain_on_timeout=True
        )
        task_id = data.get("id")
        if not task_id:
            raise self.unexpected(data, "submit response without an id")
        return SubmitResult(task_ref=str(task_id), accepted=True, message="Accepted by Runway.")

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/v1/tasks/{task_ref}")
        status = str(data.get("status", "")).upper()

        if status == "SUCCEEDED":
            outputs = data.get("output") or []
            url = outputs[0] if outputs else None
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Runway reported success but returned no output URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if status == "FAILED":
            reason = str(data.get("failure") or data.get("failureCode") or "no reason given")
            lowered = reason.lower()
            error_class = (
                ErrorClass.MODERATION
                if any(word in lowered for word in ("safety", "moderation", "content"))
                else ErrorClass.TRANSIENT
            )
            return TaskStatus(
                state="failed", message=reason, error_class=error_class, raw=data
            )

        if status in ("PENDING", "THROTTLED"):
            return TaskStatus(
                state="pending",
                message="Queued by Runway." if status == "PENDING" else "Throttled; waiting for a free slot.",
                raw=data,
            )
        if status == "RUNNING":
            progress = data.get("progress")
            return TaskStatus(
                state="running",
                progress=float(progress) if isinstance(progress, (int, float)) else None,
                raw=data,
            )
        if status == "CANCELLED":
            return TaskStatus(
                state="failed",
                message="The task was cancelled at Runway.",
                error_class=ErrorClass.UNKNOWN,
                raw=data,
            )
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)
