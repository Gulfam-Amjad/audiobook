"""PixVerse image-to-video.

Sources, all read 2026-10-28:

* Docs index - https://docs.platform.pixverse.ai/llms.txt
* Upload - https://docs.platform.pixverse.ai/upload-image-13016631e0
* Task status - https://docs.platform.pixverse.ai/query-task-status-43051212e0.md
* API pricing - https://docs.platform.pixverse.ai/pixverse-api-pricing-2394995m0
* Concurrency - https://docs.platform.pixverse.ai/rate-limit-796040m0

PixVerse is the one site in this set with a published recurring daily
allowance, but the docs state plainly that "API credits cannot be used with
the PixVerse Web" and that the memberships are separate. The 30 free credits
a day therefore belong to manual-assist, not to this adapter.
"""

from __future__ import annotations

import uuid

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
from app.providers.http_adapter import HttpProviderAdapter, read_image

MAX_IMAGE_BYTES = 20 * 1024 * 1024
#: Verified: 225 credits = $1.
USD_PER_CREDIT = 1 / 225
_PRICE_SOURCE = "https://docs.platform.pixverse.ai/pixverse-api-pricing-2394995m0"

#: Credits per second of output, V6 without audio.
_V6_CREDITS = {"360p": 5.0, "540p": 7.0, "720p": 9.0, "1080p": 18.0}

_V6_PRICE = PriceSpec(
    mode="per_second",
    usd_per_second={
        res: round(credits * USD_PER_CREDIT, 6) for res, credits in _V6_CREDITS.items()
    },
    units_per_second=dict(_V6_CREDITS),
    unit_name="credits",
    usd_per_unit=round(USD_PER_CREDIT, 6),
    confidence="verified",
    source_url=_PRICE_SOURCE,
    checked_on="2026-10-28",
    notes=(
        "Credits per second of output with audio off; 225 credits = $1. Enabling audio "
        "raises it to 7/9/12/23 credits per second."
    ),
)

#: Documented task states.
STATE_DONE = 1
STATE_GENERATING = 5
STATE_MODERATED = 7
STATE_FAILED = 8


class PixVerseProvider(HttpProviderAdapter):
    key = "pixverse"
    kind = ProviderKind.API
    base_url = "https://app-api.pixverse.ai"

    def auth_headers(self) -> dict[str, str]:
        return {"API-KEY": self.credentials.get("api_key", "")}

    def _trace_headers(self) -> dict[str, str]:
        """Every PixVerse call needs its own Ai-trace-id."""
        return {"Ai-trace-id": str(uuid.uuid4()), "Content-Type": "application/json"}

    def capabilities(self) -> ProviderCapabilities:
        resolutions = tuple(_V6_CREDITS)
        return ProviderCapabilities(
            key=self.key,
            display_name="PixVerse",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="v6",
                    label="PixVerse V6",
                    durations=(5, 8),
                    resolutions=resolutions,
                    aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4"),
                    supports_negative_prompt=True,
                    supports_seed=True,
                    supports_audio=True,
                    price=_V6_PRICE,
                ),
                ModelSpec(
                    id="v5-fast",
                    label="PixVerse V5 Fast",
                    durations=(5,),
                    resolutions=("360p", "540p", "720p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_negative_prompt=True,
                    price=PriceSpec(
                        mode="unknown",
                        unit_name="credits",
                        usd_per_unit=round(USD_PER_CREDIT, 6),
                        confidence="unknown",
                        source_url=_PRICE_SOURCE,
                        checked_on="2026-10-28",
                        notes=(
                            "The pricing table read during research covered V6 and C1 "
                            "only; no rate was captured for V5 Fast."
                        ),
                    ),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=MAX_IMAGE_BYTES,
                max_side_px=10000,
                accepts_public_url=True,
                accepts_base64=False,
                accepts_multipart_upload=True,
                requires_preupload=True,
                notes=(
                    "Under 20 MB with no side over 10,000 px. Sent as a multipart file "
                    "or an image_url; base64 is not supported."
                ),
            ),
            required_credentials=("api_key",),
            default_concurrency=3,
            default_requests_per_minute=None,
            min_poll_interval_s=5,
            result_url_ttl_hours=None,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://docs.platform.pixverse.ai/llms.txt",
            signup_url="https://platform.pixverse.ai",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "A \"Free\" API tier exists with three concurrent tasks, but whether it "
                "includes any credits is unverified. The website's 60 starting credits "
                "plus 30 a day are web-only: the docs state \"API credits cannot be used "
                "with the PixVerse Web\" and the memberships are separate. Use "
                "manual-assist for the website allowance (checked 2026-10-28)."
            ),
            automation_note=(
                "The documented API is the sanctioned route. The consumer terms forbid "
                "any \"device, program, or algorithm, including spiders, robots\" from "
                "accessing the service, and the community guidelines say each user may "
                "only have one account."
            ),
            checked_on="2026-10-28",
            endpoint_verified=False,
            integration_notes=(
                "The API host used here was not confirmed during research - only the "
                "paths were. Check the host against the official docs before enabling.",
                "Generation needs two calls: the image is uploaded first to obtain an "
                "img_id, then the generation request references it.",
                "A clip refused by moderation (state 7) is documented as refunded.",
                "No rate for V5 Fast was captured, so jobs using it will be refused by "
                "the spending guard until a price is recorded.",
            ),
        )

    async def _upload_image(self, request: GenerationRequest) -> int:
        data, mime = read_image(request.image_path, max_bytes=MAX_IMAGE_BYTES)
        response = await self.request_raw(
            "POST",
            "/openapi/v2/image/upload",
            files={"image": (f"job-{request.job_id}", data, mime)},
            headers={"Ai-trace-id": str(uuid.uuid4())},
        )
        payload = response.json()
        img_id = (payload.get("Resp") or {}).get("img_id")
        if not img_id:
            raise self.unexpected(payload, "upload response without an img_id")
        return int(img_id)

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        img_id = await self._upload_image(request)
        payload: dict[str, object] = {
            "img_id": img_id,
            "prompt": request.prompt,
            "model": request.model_id,
            "duration": request.duration_s,
            "quality": request.resolution,
            "aspect_ratio": request.aspect_ratio,
        }
        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            payload["seed"] = request.seed

        data = await self.request_json(
            "POST",
            "/openapi/v2/video/img/generate",
            json_body=payload,
            headers=self._trace_headers(),
            uncertain_on_timeout=True,
        )
        self._raise_on_error_code(data)
        video_id = (data.get("Resp") or {}).get("video_id")
        if not video_id:
            raise self.unexpected(data, "generate response without a video_id")
        return SubmitResult(
            task_ref=str(video_id), accepted=True, message="Queued by PixVerse."
        )

    def _raise_on_error_code(self, data: dict) -> None:
        """PixVerse signals refusals inside a 200 response."""
        code = data.get("ErrCode")
        if code in (None, 0):
            return
        message = str(data.get("ErrMsg") or f"ErrCode {code}")
        lowered = message.lower()
        if any(word in lowered for word in ("credit", "balance", "insufficient", "quota")):
            error_class = ErrorClass.INSUFFICIENT_CREDIT
        elif any(word in lowered for word in ("key", "auth", "token", "permission")):
            error_class = ErrorClass.AUTH
        elif any(word in lowered for word in ("moderation", "sensitive", "policy")):
            error_class = ErrorClass.MODERATION
        else:
            error_class = ErrorClass.INVALID_INPUT
        raise ProviderError(f"PixVerse refused the request: {message}", error_class, raw=data)

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json(
            "GET",
            f"/openapi/v2/video/result/{task_ref}",
            headers={"Ai-trace-id": str(uuid.uuid4())},
        )
        self._raise_on_error_code(data)
        resp = data.get("Resp") or {}
        status = resp.get("status")

        if status == STATE_DONE:
            url = resp.get("url")
            if not url:
                return TaskStatus(
                    state="failed",
                    message="PixVerse reported completion but returned no URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if status == STATE_GENERATING:
            return TaskStatus(state="running", message="Generating.", raw=data)
        if status == STATE_MODERATED:
            return TaskStatus(
                state="failed",
                message=(
                    "PixVerse blocked this clip on content moderation. Their docs say "
                    "such tasks are refunded."
                ),
                error_class=ErrorClass.MODERATION,
                raw=data,
            )
        if status == STATE_FAILED:
            return TaskStatus(
                state="failed",
                message=str(resp.get("msg") or "PixVerse reported a failure."),
                error_class=ErrorClass.TRANSIENT,
                raw=data,
            )
        return TaskStatus(
            state="unknown", message=f"Unrecognised status '{status}'.", raw=data
        )
