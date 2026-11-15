"""Replicate, used as an aggregator for Wan image-to-video models.

Sources, all read 2026-10-28:

* Model and inputs - https://replicate.com/wan-video/wan-2.2-i2v-fast/llms.txt
* 480p price - https://replicate.com/blog/wan-22
* Per-second models - https://replicate.com/pricing
* Catalog - https://replicate.com/collections/image-to-video

Replicate bills Wan 2.2 i2v Fast per video rather than per second, and the
clip length is set by ``num_frames`` at 16 fps, so the adapter converts the
requested duration into frames.
"""

from __future__ import annotations

from app.constants import ErrorClass, ProviderKind
from app.providers.base import (
    AuthResult,
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

#: Wan counts frames at 16 fps; 81 frames is the documented recommendation
#: and works out to just over five seconds.
FPS = 16
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ReplicateProvider(HttpProviderAdapter):
    key = "replicate"
    kind = ProviderKind.API
    base_url = "https://api.replicate.com"
    auth_probe_path = "/v1/account"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_token', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            key=self.key,
            display_name="Replicate",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="wan-video/wan-2.2-i2v-fast",
                    label="Wan 2.2 i2v Fast",
                    durations=(5,),
                    resolutions=("480p", "720p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_negative_prompt=True,
                    supports_seed=True,
                    supports_end_frame=True,
                    price=PriceSpec(
                        mode="per_video",
                        usd_per_video={"480p": 0.05, "720p": 0.11},
                        unit_name="usd",
                        confidence="estimated",
                        source_url="https://replicate.com/blog/wan-22",
                        checked_on="2026-10-28",
                        notes=(
                            "The 480p price of $0.05 per video is from Replicate's own "
                            "blog. The 720p figure of $0.11 comes from Pruna, the "
                            "optimiser behind the model, because Replicate's model page "
                            "did not render a price - treat it as unverified."
                        ),
                    ),
                    extra_settings={
                        "num_frames": {
                            "label": "Frames (16 fps)",
                            "type": "int",
                            "default": 81,
                        }
                    },
                ),
                ModelSpec(
                    id="wavespeedai/wan-2.1-i2v-720p",
                    label="Wan 2.1 i2v 720p",
                    durations=(5,),
                    resolutions=("720p",),
                    aspect_ratios=("16:9", "9:16"),
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=PriceSpec(
                        mode="per_second",
                        usd_per_second={"720p": 0.25},
                        unit_name="usd",
                        confidence="verified",
                        source_url="https://replicate.com/pricing",
                        checked_on="2026-10-28",
                        notes="Billed per second of output, which makes it the expensive option here.",
                    ),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=MAX_IMAGE_BYTES,
                accepts_public_url=True,
                accepts_base64=True,
                notes="Input limits are not documented centrally; each model page differs.",
            ),
            required_credentials=("api_token",),
            default_concurrency=1,
            default_requests_per_minute=None,
            min_poll_interval_s=5,
            result_url_ttl_hours=None,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://replicate.com/wan-video/wan-2.2-i2v-fast/api",
            signup_url="https://replicate.com/account/api-tokens",
            pricing_url="https://replicate.com/pricing",
            free_tier_note=(
                "No free credits were verified on any official Replicate page. Assume "
                "that credit must be bought before anything runs (checked 2026-10-28)."
            ),
            automation_note=(
                "Replicate is an API product; programmatic use is its purpose and "
                "nothing read during research restricts it."
            ),
            checked_on="2026-10-28",
            endpoint_verified=False,
            integration_notes=(
                "The prediction request shape used here follows Replicate's standard "
                "/v1/models/{owner}/{name}/predictions form, which was not confirmed "
                "against the model page during research. Verify before trusting it with "
                "money.",
                "Rate limits, result URL lifetime and free credits are all unverified.",
                "Clip length is controlled by num_frames at 16 fps rather than a "
                "duration field, so a 5-second request is sent as 81 frames.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        frames = int(request.extra.get("num_frames") or (request.duration_s * FPS + 1))
        payload: dict[str, object] = {
            "input": {
                "image": data_uri(request.image_path, max_bytes=MAX_IMAGE_BYTES),
                "prompt": request.prompt,
                "num_frames": frames,
                "resolution": request.resolution,
            }
        }
        if request.negative_prompt:
            payload["input"]["negative_prompt"] = request.negative_prompt  # type: ignore[index]
        if request.seed is not None:
            payload["input"]["seed"] = request.seed  # type: ignore[index]

        data = await self.request_json(
            "POST",
            f"/v1/models/{request.model_id}/predictions",
            json_body=payload,
            uncertain_on_timeout=True,
        )
        prediction_id = data.get("id")
        if not prediction_id:
            raise self.unexpected(data, "prediction response without an id")
        return SubmitResult(
            task_ref=str(prediction_id), accepted=True, message="Queued by Replicate."
        )

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/v1/predictions/{task_ref}")
        status = str(data.get("status") or "").lower()

        if status == "succeeded":
            url = self._extract_video_url(data.get("output"))
            if not url:
                return TaskStatus(
                    state="failed",
                    message="Replicate reported success but returned no video URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            metrics = data.get("metrics") or {}
            return TaskStatus(
                state="succeeded",
                video_url=url,
                progress=1.0,
                actual_cost_usd=(
                    float(metrics["total_cost"])
                    if isinstance(metrics, dict) and isinstance(metrics.get("total_cost"), (int, float))
                    else None
                ),
                raw=data,
            )

        if status in ("failed", "canceled"):
            reason = str(data.get("error") or f"Prediction {status}.")
            return TaskStatus(
                state="failed", message=reason, error_class=ErrorClass.TRANSIENT, raw=data
            )
        if status == "starting":
            return TaskStatus(state="pending", message="Starting.", raw=data)
        if status == "processing":
            return TaskStatus(state="running", message="Generating.", raw=data)
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)

    @staticmethod
    def _extract_video_url(output: object) -> str | None:
        if isinstance(output, str):
            return output
        if isinstance(output, list) and output and isinstance(output[0], str):
            return output[0]
        if isinstance(output, dict):
            for field in ("video", "url", "output"):
                value = output.get(field)
                if isinstance(value, str):
                    return value
        return None

    async def check_auth(self) -> AuthResult:
        """Replicate exposes an account endpoint, which is a free, safe probe."""
        if not self.credentials.get("api_token"):
            return AuthResult(False, "Missing credential: api_token", reached_provider=False)
        try:
            data = await self.request_json("GET", "/v1/account")
        except ProviderError as exc:
            if exc.error_class == ErrorClass.AUTH:
                return AuthResult(False, "Replicate rejected the API token.")
            return AuthResult(False, exc.message, reached_provider=False)
        username = data.get("username") or data.get("name") or "unknown account"
        return AuthResult(True, f"Replicate accepted the token (account: {username}).")
