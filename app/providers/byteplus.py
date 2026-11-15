"""BytePlus ModelArk (Seedance) image-to-video.

Sources, all read 2026-10-28:

* Pricing - https://www.byteplus.com/en/product/modelark/pricing
* Regional pricing - https://ai.byteplus.com/ark/region:ap-southeast-1/docs/model-pricing
* Docs - https://docs.byteplus.com/en/docs/ModelArk/1544106

ModelArk bills by token, where tokens are
``(input video seconds + output seconds) x width x height x fps / 1024``.
The per-second prices below are derived from BytePlus's own worked example -
a 720p 16:9 clip at 1248x704, 24 fps, 5 s is 102,960 tokens and costs $0.10
on Seedance 1.0 Pro Fast - rather than from a published per-second rate.
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

#: From the official worked example: 720p/5s = 102,960 tokens.
TOKENS_PER_SECOND_720P = 102_960 / 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PRICE_SOURCE = "https://www.byteplus.com/en/product/modelark/pricing"


def _from_token_price(usd_per_million_tokens: float) -> float:
    return round(TOKENS_PER_SECOND_720P * usd_per_million_tokens / 1_000_000, 6)


def _price(usd_per_million_tokens: float, note: str) -> PriceSpec:
    per_second = _from_token_price(usd_per_million_tokens)
    return PriceSpec(
        mode="per_second",
        usd_per_second={"720p": per_second},
        units_per_second={"720p": TOKENS_PER_SECOND_720P},
        unit_name="tokens",
        usd_per_unit=usd_per_million_tokens / 1_000_000,
        # The token rate is verified; turning it into a per-second figure
        # assumes the official example's dimensions and frame rate.
        confidence="estimated",
        source_url=_PRICE_SOURCE,
        checked_on="2026-10-28",
        notes=(
            f"${usd_per_million_tokens} per million tokens (verified), converted using "
            f"BytePlus's own 720p example of {TOKENS_PER_SECOND_720P:,.0f} tokens per "
            f"second. A different frame rate or pixel count changes the real cost. {note}"
        ).strip(),
    )


class BytePlusSeedanceProvider(HttpProviderAdapter):
    key = "byteplus"
    kind = ProviderKind.API
    base_url = "https://ark.ap-southeast.bytepluses.com"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        ratios = ("16:9", "9:16", "1:1", "4:3")
        return ProviderCapabilities(
            key=self.key,
            display_name="BytePlus ModelArk (Seedance)",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="seedance-1-0-pro-fast-251015",
                    label="Seedance 1.0 Pro Fast",
                    durations=(5, 10),
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    price=_price(
                        1.0,
                        "The cheapest verified route to a 720p clip in this whole set, at "
                        "about $0.10 per 5 seconds online.",
                    ),
                ),
                ModelSpec(
                    id="seedance-1-0-pro-250528",
                    label="Seedance 1.0 Pro",
                    durations=(5, 10),
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    price=_price(2.5, "Carries a one-off 2,000k-token free allowance."),
                ),
                ModelSpec(
                    id="seedance-1-5-pro-251215",
                    label="Seedance 1.5 Pro (silent)",
                    durations=(5, 10),
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    price=_price(1.2, "Carries a one-off 2,000k-token free allowance."),
                    notes="BytePlus lists this model as retiring on 2026-11-11.",
                    deprecated=True,
                ),
                ModelSpec(
                    id="dreamina-seedance-2-0-mini-260615",
                    label="Dreamina Seedance 2.0 Mini",
                    durations=(5, 10),
                    resolutions=("720p",),
                    aspect_ratios=ratios,
                    price=_price(3.5, "No free allowance is listed for the 2.x models."),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=MAX_IMAGE_BYTES,
                accepts_public_url=True,
                accepts_base64=True,
                notes="Input image limits were not verified during research.",
            ),
            required_credentials=("api_key",),
            default_concurrency=1,
            default_requests_per_minute=None,
            min_poll_interval_s=10,
            result_url_ttl_hours=None,
            supports_webhook=False,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://docs.byteplus.com/en/docs/ModelArk/1544106",
            signup_url="https://console.byteplus.com/ark",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "A one-off 2,000k-token allowance each for Seedance 1.5 Pro and 1.0 Pro, "
                "which is roughly nineteen 720p 5-second clips in total. The table read "
                "during research showed no free allowance for 1.0 Pro Fast or for the "
                "2.x models. Not recurring (checked 2026-10-28)."
            ),
            automation_note=(
                "ModelArk is an API product and nothing read during research restricts "
                "automated use of it."
            ),
            checked_on="2026-10-28",
            endpoint_verified=False,
            integration_notes=(
                "Neither the API host nor the request path was confirmed during "
                "research. Verify both against the ModelArk documentation before "
                "enabling this adapter.",
                "Billing is per token, not per second. The per-second prices shown here "
                "are derived from one official 720p example and will be wrong for any "
                "clip with a different pixel count or frame rate.",
                "An \"offline\" or flex mode costs about half as much with no latency "
                "guarantee; this adapter does not request it.",
                "Rate limits are set per model on the console's model activation page "
                "rather than published centrally.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        # ModelArk takes generation parameters as text directives appended to
        # the prompt content rather than as separate JSON fields.
        directives = (
            f"--resolution {request.resolution} "
            f"--duration {request.duration_s} "
            f"--ratio {request.aspect_ratio}"
        )
        payload = {
            "model": request.model_id,
            "content": [
                {"type": "text", "text": f"{request.prompt} {directives}".strip()},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": data_uri(request.image_path, max_bytes=MAX_IMAGE_BYTES)
                    },
                    "role": "first_frame",
                },
            ],
        }

        data = await self.request_json(
            "POST",
            "/api/v3/contents/generations/tasks",
            json_body=payload,
            uncertain_on_timeout=True,
        )
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            raise self.unexpected(data, "submit response without a task id")
        return SubmitResult(
            task_ref=str(task_id), accepted=True, message="Queued by ModelArk."
        )

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/api/v3/contents/generations/tasks/{task_ref}")
        status = str(data.get("status") or "").lower()
        usage = data.get("usage") or {}
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None

        if status in ("succeeded", "success"):
            content = data.get("content") or {}
            url = content.get("video_url") if isinstance(content, dict) else None
            if not url:
                return TaskStatus(
                    state="failed",
                    message="ModelArk reported success but returned no video_url.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(
                state="succeeded",
                video_url=str(url),
                progress=1.0,
                actual_units=float(total_tokens) if isinstance(total_tokens, (int, float)) else None,
                raw=data,
            )

        if status == "failed":
            error = data.get("error") or {}
            reason = str(
                (error.get("message") if isinstance(error, dict) else None)
                or "ModelArk reported a failure."
            )
            lowered = reason.lower()
            if any(word in lowered for word in ("quota", "balance", "insufficient")):
                error_class = ErrorClass.INSUFFICIENT_CREDIT
            elif any(word in lowered for word in ("risk", "sensitive", "policy")):
                error_class = ErrorClass.MODERATION
            else:
                error_class = ErrorClass.TRANSIENT
            return TaskStatus(state="failed", message=reason, error_class=error_class, raw=data)

        if status == "queued":
            return TaskStatus(state="pending", message="Queued by ModelArk.", raw=data)
        if status == "running":
            return TaskStatus(state="running", message="Generating.", raw=data)
        if status == "cancelled":
            return TaskStatus(
                state="failed",
                message="The task was cancelled at ModelArk.",
                error_class=ErrorClass.UNKNOWN,
                raw=data,
            )
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)
