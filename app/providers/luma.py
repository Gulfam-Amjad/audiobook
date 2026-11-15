"""Luma Ray 3.2 image-to-video, via the Luma Agents API.

Sources, all read 2026-10-28:

* Generation guide - https://docs.agents.lumalabs.ai/guides/videos/generation/
* Pricing - https://lumalabs.ai/api/pricing
* FAQ (no free tier) - https://docs.agents.lumalabs.ai/guides/faq/

Luma prices per clip rather than per second, and the 10-second rate is not
twice the 5-second rate, so both lengths are quoted explicitly instead of
being flattened into one per-second figure.
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
from app.providers.http_adapter import HttpProviderAdapter, encode_data_uri, read_image

MAX_IMAGE_BYTES = 50 * 1024 * 1024
_PRICE_SOURCE = "https://lumalabs.ai/api/pricing"

_RAY_PRICE = PriceSpec(
    mode="per_video",
    usd_per_video={
        "5s@540p": 0.15,
        "5s@720p": 0.30,
        "5s@1080p": 1.20,
        "10s@540p": 0.45,
        "10s@720p": 0.90,
        "10s@1080p": 3.60,
    },
    unit_name="usd",
    confidence="verified",
    source_url=_PRICE_SOURCE,
    checked_on="2026-10-28",
    notes=(
        "Priced per clip; the 10s rate is three times the 5s rate, not twice. "
        "HDR doubles the price and HDR plus EXR triples it."
    ),
)


class LumaProvider(HttpProviderAdapter):
    key = "luma"
    kind = ProviderKind.API
    base_url = "https://agents.lumalabs.ai"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            key=self.key,
            display_name="Luma (Ray 3.2)",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="ray-3.2",
                    label="Ray 3.2",
                    durations=(5, 10),
                    # 360p exists as a draft mode but is not priced on the
                    # pricing page, so it is left out rather than guessed at.
                    resolutions=("540p", "720p", "1080p"),
                    aspect_ratios=("9:16", "3:4", "1:1", "4:3", "16:9", "21:9"),
                    supports_end_frame=True,
                    price=_RAY_PRICE,
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp", "gif"}),
                max_bytes=MAX_IMAGE_BYTES,
                max_side_px=8000,
                accepts_public_url=True,
                accepts_base64=True,
                notes="A supplied URL must respond within about 18 seconds.",
            ),
            required_credentials=("api_key",),
            default_concurrency=1,
            default_requests_per_minute=20,
            min_poll_interval_s=10,
            result_url_ttl_hours=1,
            supports_webhook=False,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://docs.agents.lumalabs.ai/guides/videos/generation/",
            signup_url="https://platform.lumalabs.ai",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "No free tier. Luma's own FAQ says \"the API is pay-as-you-go from the "
                "first image\" (checked 2026-10-28). The consumer site has no free plan "
                "either, so there is nothing to claim here."
            ),
            automation_note=(
                "The documented API is the sanctioned route. Luma's terms (m) prohibit "
                "accessing the service through crawlers or agents, and also bar using "
                "non-public APIs, which rules out driving the website."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Result URLs are presigned and expire after about an hour, so a download "
                "must follow promptly; each poll issues a fresh one.",
                "A 10-second clip is rejected if it uses start_frame, so this adapter "
                "sends the source image as a keyframe for 10s and as start_frame for 5s.",
                "Concurrency and request limits depend on your plan and are reported in "
                "the X-RateLimit-Limit header rather than published; the value here is a "
                "conservative default, not a verified figure.",
            ),
        )

    def _frame_payload(self, request: GenerationRequest) -> dict[str, object]:
        data, mime = read_image(request.image_path, max_bytes=MAX_IMAGE_BYTES)
        return {
            "type": "image",
            "data": encode_data_uri(data, mime),
            "media_type": mime,
        }

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        if request.duration_s not in (5, 10):
            raise ProviderError(
                f"Luma accepts 5s or 10s clips, not {request.duration_s}s.",
                ErrorClass.INVALID_INPUT,
            )

        frame = self._frame_payload(request)
        video: dict[str, object] = {
            "resolution": request.resolution,
            "duration": f"{request.duration_s}s",
            "aspect_ratio": request.aspect_ratio,
        }
        if request.duration_s == 10:
            # start_frame is rejected for 10s clips; keyframes are the
            # documented way to seed a longer generation from one image.
            video["keyframes"] = [frame]
        else:
            video["start_frame"] = frame

        payload = {
            "model": request.model_id,
            "type": "video",
            "prompt": request.prompt,
            "video": video,
        }

        data = await self.request_json(
            "POST", "/v1/generations", json_body=payload, uncertain_on_timeout=True
        )
        generation_id = data.get("id")
        if not generation_id:
            raise self.unexpected(data, "submit response without an id")
        return SubmitResult(
            task_ref=str(generation_id), accepted=True, message="Queued by Luma."
        )

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/v1/generations/{task_ref}")
        state = str(data.get("state") or data.get("status") or "").lower()

        if state in ("completed", "succeeded"):
            outputs = data.get("output") or []
            url = None
            if isinstance(outputs, list) and outputs:
                first = outputs[0]
                url = first.get("url") if isinstance(first, dict) else first
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Luma reported completion but returned no output URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if state == "failed":
            reason = str(data.get("failure_reason") or "no reason given")
            return TaskStatus(
                state="failed",
                message=reason,
                error_class=ErrorClass.TRANSIENT,
                raw=data,
            )

        if state in ("queued", "pending"):
            return TaskStatus(state="pending", message="Queued by Luma.", raw=data)
        if state in ("dreaming", "running", "processing"):
            return TaskStatus(state="running", message="Generating.", raw=data)
        return TaskStatus(state="unknown", message=f"Unrecognised state '{state}'.", raw=data)
