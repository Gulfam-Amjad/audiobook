"""Pika 2.5 image-to-video, via the self-serve "Pika API Club".

Sources, all read 2026-10-28:

* Docs index - https://dev.pika.art/llms.txt
* Endpoint spec - https://dev.pika.art/llms/pika/pika-2.5/image-to-video
* Pricing - https://dev.pika.art/pricing
* Launch post - https://pika.art/blog/pika-api-club

Pika accepts a URL for the source frame, not an upload, so this adapter does
the documented two-step: ask for a presigned PUT, upload the bytes, then
submit the resulting Pika URL.
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
from app.providers.http import build_client
from app.providers.http_adapter import HttpProviderAdapter, read_image

_PRICE_SOURCE = "https://dev.pika.art/pricing"

#: Model id -> generation path. Each model has its own endpoint rather than a
#: model field in the body, so the mapping is explicit.
ENDPOINTS: dict[str, str] = {
    "pika-2.5": "/v1/media/pika/pika-2.5/image-to-video",
}

_PIKA_PRICE = PriceSpec(
    mode="per_second",
    usd_per_second={"480p": 0.02, "720p": 0.04, "1080p": 0.06},
    unit_name="usd",
    confidence="verified",
    source_url=_PRICE_SOURCE,
    checked_on="2026-10-28",
    notes=(
        "Per second of output. The same page separately lists \"1080p 5s: $0.09 per "
        "second\", which contradicts the $0.06 rate; the lower figure is used here, so "
        "treat 1080p as possibly underestimated. Only successful generations are billed."
    ),
)


class PikaProvider(HttpProviderAdapter):
    key = "pika"
    kind = ProviderKind.API
    base_url = "https://api.dev.pika.art"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            key=self.key,
            display_name="Pika",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="pika-2.5",
                    label="Pika 2.5",
                    durations=(5, 10),
                    resolutions=("480p", "720p", "1080p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    price=_PIKA_PRICE,
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                accepts_public_url=True,
                accepts_base64=False,
                requires_preupload=True,
                notes=(
                    "Pika takes a URL only. Local files go through POST /v1/media/uploads, "
                    "which returns a presigned PUT valid for five minutes. Accepted "
                    "formats and size limits are not published."
                ),
            ),
            required_credentials=("api_key",),
            default_concurrency=1,
            default_requests_per_minute=None,
            min_poll_interval_s=10,
            result_url_ttl_hours=None,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://dev.pika.art/llms.txt",
            signup_url="https://dev.pika.art",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "Pika states plainly: \"no free generation tier and no separate test "
                "environment\". Membership is $10 a month and includes a $10 credit for "
                "the first month, so the first month is effectively prepaid rather than "
                "free. The catalog and price-quote endpoints cost nothing to call "
                "(checked 2026-10-28)."
            ),
            automation_note=(
                "The documented API is the sanctioned route. Pika's terms ban bots and "
                "scripts, and explicitly forbid creating multiple accounts to get extra "
                "free or trial access."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Rate limits exist (HTTP 429 'rate_limited') but the numbers are not "
                "published, so the conservative default of one job at a time is used.",
                "How long a result URL stays valid is not documented; download promptly.",
                "The request field names for the generation call were not confirmed "
                "against a worked example, so the first real submission should be "
                "treated as a test.",
            ),
        )

    async def _upload_image(self, request: GenerationRequest) -> str:
        """Push the source frame to Pika's storage and return its URL."""
        data, mime = read_image(request.image_path)
        ticket = await self.request_json(
            "POST",
            "/v1/media/uploads",
            json_body={"content_type": mime, "filename": f"job-{request.job_id}"},
        )
        put_url = ticket.get("upload_url") or ticket.get("url")
        public_url = ticket.get("public_url") or ticket.get("media_url") or ticket.get("asset_url")
        if not put_url or not public_url:
            raise self.unexpected(ticket, "upload ticket without both a PUT and a public URL")

        # The presigned PUT goes straight to object storage, so it must not
        # carry this adapter's Authorization header.
        async with build_client() as plain:
            response = await plain.put(
                str(put_url), content=data, headers={"Content-Type": mime}
            )
        if not response.is_success:
            raise ProviderError(
                f"Uploading the source frame to Pika failed with HTTP {response.status_code}.",
                ErrorClass.TRANSIENT,
            )
        return str(public_url)

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        path = ENDPOINTS.get(request.model_id)
        if path is None:
            raise ProviderError(
                f"This adapter has no endpoint recorded for Pika model "
                f"'{request.model_id}'.",
                ErrorClass.INVALID_INPUT,
            )

        image_url = await self._upload_image(request)
        payload = {
            "image": image_url,
            "promptText": request.prompt,
            "duration": request.duration_s,
            "resolution": request.resolution,
            "aspectRatio": request.aspect_ratio,
        }
        if request.negative_prompt:
            payload["negativePrompt"] = request.negative_prompt

        data = await self.request_json(
            "POST", path, json_body=payload, uncertain_on_timeout=True
        )
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise self.unexpected(data, "submit response without a job id")
        return SubmitResult(task_ref=str(job_id), accepted=True, message="Queued by Pika.")

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/v1/media/jobs/{task_ref}")
        state = str(data.get("status") or data.get("state") or "").lower()

        if state == "completed":
            output = data.get("output") or {}
            video = output.get("video") if isinstance(output, dict) else None
            url = video.get("url") if isinstance(video, dict) else None
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Pika reported completion but returned no video URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if state == "failed":
            reason = str(data.get("error") or data.get("message") or "no reason given")
            return TaskStatus(
                state="failed", message=reason, error_class=ErrorClass.TRANSIENT, raw=data
            )
        if state == "queued":
            return TaskStatus(state="pending", message="Queued by Pika.", raw=data)
        if state == "running":
            return TaskStatus(state="running", message="Generating.", raw=data)
        return TaskStatus(state="unknown", message=f"Unrecognised status '{state}'.", raw=data)
