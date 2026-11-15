"""Alibaba Cloud Model Studio (Wan) image-to-video.

Sources, all read 2026-10-28:

* Model pricing - https://www.alibabacloud.com/help/en/model-studio/model-pricing
* Free quota terms - https://www.alibabacloud.com/help/en/model-studio/new-free-quota
* API reference - https://www.alibabacloud.com/help/en/model-studio/image-to-video-api-reference

This is the only provider in the set with a genuine, if one-off, free API
allowance: 50 seconds per Wan image-to-video model, Singapore
"International" scope, valid 90 days. That is about ten five-second clips per
model, once - not a recurring daily allowance.
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

#: Wan 2.1/2.2 cap images at 10 MB; 2.5/2.6 allow 20 MB. The lower figure is
#: used so a job cannot be rejected after the bytes have been uploaded.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PRICE_SOURCE = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"


def _price(rates: dict[str, float], note: str) -> PriceSpec:
    return PriceSpec(
        mode="per_second",
        usd_per_second=rates,
        unit_name="usd",
        confidence="verified",
        source_url=_PRICE_SOURCE,
        checked_on="2026-10-28",
        notes=f"Singapore region, per second of output. {note}".strip(),
    )


#: DashScope is split by region and the two halves do not share accounts,
#: keys, pricing or free quota. The international host is the default because
#: the free allowance documented above is the Singapore one.
REGION_HOSTS = {
    "singapore": "https://dashscope-intl.aliyuncs.com",
    "international": "https://dashscope-intl.aliyuncs.com",
    "beijing": "https://dashscope.aliyuncs.com",
    "china": "https://dashscope.aliyuncs.com",
}
DEFAULT_REGION = "singapore"


class AlibabaModelStudioProvider(HttpProviderAdapter):
    key = "alibaba"
    kind = ProviderKind.API
    base_url = REGION_HOSTS[DEFAULT_REGION]

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        region = (self.credentials.get("region") or DEFAULT_REGION).strip().lower()
        # An unrecognised region falls back to the documented default rather
        # than building a URL out of whatever was typed into .env.
        self.region = region if region in REGION_HOSTS else DEFAULT_REGION
        self.base_url = REGION_HOSTS[self.region]

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        ratios = ("16:9", "9:16", "1:1")
        return ProviderCapabilities(
            key=self.key,
            display_name="Alibaba Model Studio (Wan)",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="wan2.2-i2v-flash",
                    label="Wan 2.2 i2v Flash",
                    durations=(5,),
                    resolutions=("480p", "720p"),
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=_price(
                        {"480p": 0.015, "720p": 0.036},
                        "The cheapest Wan option and the one the free quota covers.",
                    ),
                ),
                ModelSpec(
                    id="wan2.2-i2v-plus",
                    label="Wan 2.2 i2v Plus",
                    durations=(5,),
                    resolutions=("480p",),
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=_price({"480p": 0.02}, ""),
                ),
                ModelSpec(
                    id="wan2.6-i2v-flash",
                    label="Wan 2.6 i2v Flash",
                    durations=(5,),
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    supports_seed=True,
                    supports_audio=True,
                    price=_price(
                        {"720p": 0.025},
                        "Silent output; enabling audio doubles it to $0.05 per second.",
                    ),
                ),
                ModelSpec(
                    id="wan2.6-i2v",
                    label="Wan 2.6 i2v",
                    durations=(5,),
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=_price({"720p": 0.10}, ""),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "bmp", "webp"}),
                max_bytes=MAX_IMAGE_BYTES,
                min_side_px=240,
                max_side_px=8000,
                accepts_public_url=True,
                accepts_base64=True,
                notes=(
                    "240-8000 px per side. PNG must not carry an alpha channel. "
                    "10 MB for Wan 2.1/2.2, 20 MB for Wan 2.5/2.6."
                ),
            ),
            required_credentials=("api_key",),
            optional_credentials=("region",),
            default_concurrency=1,
            default_requests_per_minute=None,
            min_poll_interval_s=15,
            result_url_ttl_hours=24,
            supports_webhook=False,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://www.alibabacloud.com/help/en/model-studio/image-to-video-api-reference",
            signup_url="https://www.alibabacloud.com/en/product/modelstudio",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "50 free seconds per Wan image-to-video model - about ten 5-second clips "
                "each - in the Singapore \"International\" scope, valid 90 days and "
                "covering real-time calls only. You must complete your account "
                "information to receive it, and re-registering does not grant it again. "
                "This is a one-off trial, not a daily allowance (checked 2026-10-28)."
            ),
            automation_note=(
                "Model Studio is an API product and nothing read during research "
                "restricts automated use of it."
            ),
            checked_on="2026-10-28",
            endpoint_verified=False,
            integration_notes=(
                "The request path used here was not confirmed during research; only the "
                "polling path and the async header were. Verify the submit path against "
                "the official API reference before enabling this adapter.",
                "Turn on the console's \"Free Quota Only\" switch to stop billing the "
                "moment the trial seconds run out.",
                "Both the task id and the result URL expire after 24 hours, so a clip "
                "left undownloaded overnight is lost.",
                "Generation takes one to five minutes, which is why polling here is "
                "spaced further apart than for other providers.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        payload: dict[str, object] = {
            "model": request.model_id,
            "input": {
                "prompt": request.prompt,
                "img_url": data_uri(request.image_path, max_bytes=MAX_IMAGE_BYTES),
            },
            "parameters": {
                "resolution": request.resolution,
                "duration": request.duration_s,
                "prompt_extend": False,
            },
        }
        if request.negative_prompt:
            payload["input"]["negative_prompt"] = request.negative_prompt  # type: ignore[index]
        if request.seed is not None:
            payload["parameters"]["seed"] = request.seed  # type: ignore[index]

        data = await self.request_json(
            "POST",
            "/api/v1/services/aigc/video-generation/video-synthesis",
            json_body=payload,
            headers={"X-DashScope-Async": "enable"},
            uncertain_on_timeout=True,
        )
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise self.unexpected(data, "submit response without a task_id")
        return SubmitResult(
            task_ref=str(task_id), accepted=True, message="Queued by Model Studio."
        )

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/api/v1/tasks/{task_ref}")
        output = data.get("output") or {}
        status = str(output.get("task_status") or "").upper()

        if status == "SUCCEEDED":
            url = output.get("video_url")
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Model Studio reported success but returned no video_url.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if status == "FAILED":
            reason = str(output.get("message") or data.get("message") or "no reason given")
            lowered = reason.lower()
            if any(word in lowered for word in ("quota", "balance", "arrears", "insufficient")):
                error_class = ErrorClass.INSUFFICIENT_CREDIT
            elif any(word in lowered for word in ("sensitive", "policy", "risk")):
                error_class = ErrorClass.MODERATION
            else:
                error_class = ErrorClass.TRANSIENT
            return TaskStatus(state="failed", message=reason, error_class=error_class, raw=data)

        if status in ("PENDING", "SUSPENDED"):
            return TaskStatus(state="pending", message="Queued by Model Studio.", raw=data)
        if status == "RUNNING":
            return TaskStatus(state="running", message="Generating.", raw=data)
        if status == "UNKNOWN":
            return TaskStatus(
                state="not_found",
                message="Model Studio no longer recognises this task; ids expire after 24 hours.",
                raw=data,
            )
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)
