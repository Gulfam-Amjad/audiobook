"""Kling (Kuaishou) image-to-video.

Sources, all read 2026-10-28:

* Docs index - https://kling.ai/document-api/llms.txt
* Image-to-video example - https://app.klingai.com/global/dev/document-api/apiReference/model/imageToVideo
* Pricing (1 unit = $0.14) - https://kling.ai/dev/pricing
* Concurrency - https://kling.ai/document-api/api/get-started/concurrency-rules.md

Kling authenticates with a JWT the client signs itself from an access key and
a secret key, which is why this adapter needs two credential fields rather
than one. The token is short-lived and minted per request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

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
from app.providers.http_adapter import HttpProviderAdapter, read_image

#: Verified: one Kling "unit" lists at $0.14.
USD_PER_UNIT = 0.14
MAX_IMAGE_BYTES = 50 * 1024 * 1024
_PRICE_SOURCE = "https://kling.ai/dev/pricing"

#: Model id -> units per second of output, by resolution.
_UNIT_RATES: dict[str, dict[str, float]] = {
    "kling-2.6": {"720p": 0.3, "1080p": 0.5},
    "kling-3.0": {"720p": 0.6, "1080p": 0.8},
    "kling-3.0-turbo": {"720p": 0.8, "1080p": 1.0},
    "kling-4.0": {"720p": 1.0, "1080p": 1.5},
}

_TOKEN_LIFETIME_S = 1800


def _price(model_id: str, note: str = "") -> PriceSpec:
    units = _UNIT_RATES[model_id]
    return PriceSpec(
        mode="per_second",
        usd_per_second={
            res: round(value * USD_PER_UNIT, 6) for res, value in units.items()
        },
        units_per_second=dict(units),
        unit_name="units",
        usd_per_unit=USD_PER_UNIT,
        confidence="verified",
        source_url=_PRICE_SOURCE,
        checked_on="2026-10-28",
        notes=(
            f"{units['720p']} units per second at 720p, {units['1080p']} at 1080p, "
            f"at the list price of ${USD_PER_UNIT} per unit. {note}"
        ).strip(),
    )


def _sign_jwt(access_key: str, secret_key: str) -> str:
    """Mint the short-lived HS256 token Kling expects.

    Hand-rolled rather than pulling in a JWT library: this is the only place
    in the project that needs one, and the payload is three fields.
    """

    def segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    now = int(time.time())
    header = segment({"alg": "HS256", "typ": "JWT"})
    body = segment({"iss": access_key, "exp": now + _TOKEN_LIFETIME_S, "nbf": now - 5})
    signing_input = f"{header}.{body}".encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{header}.{body}.{encoded_signature}"


class KlingProvider(HttpProviderAdapter):
    key = "kling"
    kind = ProviderKind.API
    base_url = "https://api-singapore.klingai.com"

    def auth_headers(self) -> dict[str, str]:
        access_key = self.credentials.get("access_key", "")
        secret_key = self.credentials.get("secret_key", "")
        if not (access_key and secret_key):
            return {"Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {_sign_jwt(access_key, secret_key)}",
            "Content-Type": "application/json",
        }

    def client(self):  # noqa: ANN201 - matches the base signature
        """Rebuild the client so the signed token is never stale.

        The base class caches one client for the adapter's lifetime, which
        would pin a token that expires after half an hour.
        """
        client = super().client()
        access_key = self.credentials.get("access_key", "")
        secret_key = self.credentials.get("secret_key", "")
        if access_key and secret_key:
            client.headers["Authorization"] = f"Bearer {_sign_jwt(access_key, secret_key)}"
        return client

    def capabilities(self) -> ProviderCapabilities:
        resolutions = ("720p", "1080p")
        ratios = ("16:9", "9:16", "1:1")
        return ProviderCapabilities(
            key=self.key,
            display_name="Kling",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="kling-2.6",
                    label="Kling 2.6",
                    durations=(5, 10),
                    resolutions=resolutions,
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    supports_camera_control=True,
                    supports_end_frame=True,
                    price=_price("kling-2.6", "The cheapest Kling model."),
                ),
                ModelSpec(
                    id="kling-3.0",
                    label="Kling 3.0",
                    durations=(5, 10),
                    resolutions=resolutions,
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    supports_end_frame=True,
                    price=_price("kling-3.0", "Silent output."),
                ),
                ModelSpec(
                    id="kling-3.0-turbo",
                    label="Kling 3.0 Turbo",
                    durations=(5, 10),
                    resolutions=resolutions,
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    price=_price("kling-3.0-turbo"),
                ),
                ModelSpec(
                    id="kling-4.0",
                    label="Kling 4.0",
                    durations=(5, 10),
                    resolutions=resolutions,
                    aspect_ratios=ratios,
                    supports_negative_prompt=True,
                    price=_price(
                        "kling-4.0",
                        (
                            "Kling's own docs also quote \"$0.157/s\" for this model, "
                            "which does not reconcile with the unit price."
                        ),
                    ),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png"}),
                max_bytes=MAX_IMAGE_BYTES,
                min_side_px=300,
                min_aspect=0.4,
                max_aspect=2.5,
                accepts_public_url=True,
                accepts_base64=True,
                notes="jpg, jpeg or png up to 50 MB, at least 300 px per side.",
            ),
            required_credentials=("access_key", "secret_key"),
            default_concurrency=5,
            default_requests_per_minute=None,
            min_poll_interval_s=10,
            result_url_ttl_hours=720,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://app.klingai.com/global/dev/document-api/apiReference/model/imageToVideo",
            signup_url="https://app.klingai.com/global/dev",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "No free API credits exist. The cheapest documented entry is a trial "
                "pack. Website credits do not transfer to the API, and Kling's own "
                "credits policy says daily login credits are a subscriber-only event - "
                "so the widely repeated \"66 free credits daily\" claim contradicts the "
                "official policy and should not be planned around (checked 2026-10-28)."
            ),
            automation_note=(
                "The documented API is the sanctioned route. Kling's consumer terms "
                "3.1(h) forbid automated scripts that \"collect information from or "
                "otherwise interact with\" the service, and credits cannot be moved "
                "between accounts controlled by the same user."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Authentication is a self-signed JWT built from the access key and "
                "secret key. That scheme was not confirmed during research, so treat "
                "the first connection test as the real verification.",
                "Production access needs a prepaid pack; the smallest documented "
                "production pack is $700 for 5,000 units expiring in 180 days.",
                "Exceeding the pack's concurrency returns error 1303 rather than "
                "queueing, so keep the concurrency limit accurate.",
                "Result URLs are hotlink-protected and kept for 30 days.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        data_bytes, _ = read_image(request.image_path, max_bytes=MAX_IMAGE_BYTES)
        payload: dict[str, object] = {
            "model_name": request.model_id,
            # Kling takes bare base64 here, not a data: URI.
            "image": base64.b64encode(data_bytes).decode("ascii"),
            "prompt": request.prompt,
            "duration": str(request.duration_s),
            "aspect_ratio": request.aspect_ratio,
            "mode": "std" if request.resolution == "720p" else "pro",
        }
        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt

        data = await self.request_json(
            "POST",
            f"/image-to-video/{request.model_id}",
            json_body=payload,
            uncertain_on_timeout=True,
        )
        task_id = (data.get("data") or {}).get("task_id") or data.get("task_id")
        if not task_id:
            raise self.unexpected(data, "submit response without a task_id")
        return SubmitResult(task_ref=str(task_id), accepted=True, message="Queued by Kling.")

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", "/tasks", params={"task_ids": task_ref})
        tasks = data.get("data")
        task = tasks[0] if isinstance(tasks, list) and tasks else tasks
        if not isinstance(task, dict):
            return TaskStatus(
                state="not_found",
                message=f"Kling returned no task for id '{task_ref}'.",
                raw=data,
            )

        status = str(task.get("task_status") or "").lower()
        if status == "succeed":
            videos = ((task.get("task_result") or {}).get("videos")) or []
            url = videos[0].get("url") if videos and isinstance(videos[0], dict) else None
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Kling reported success but returned no video URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=str(url), progress=1.0, raw=data)

        if status == "failed":
            reason = str(task.get("task_status_msg") or "Kling reported a failure.")
            lowered = reason.lower()
            error_class = (
                ErrorClass.MODERATION
                if any(word in lowered for word in ("risk", "sensitive", "content"))
                else ErrorClass.TRANSIENT
            )
            return TaskStatus(state="failed", message=reason, error_class=error_class, raw=data)

        if status == "submitted":
            return TaskStatus(state="pending", message="Queued by Kling.", raw=data)
        if status == "processing":
            return TaskStatus(state="running", message="Generating.", raw=data)
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)
