"""fal.ai, used as an aggregator for several image-to-video models.

Sources, all read 2026-10-28:

* Queue API - https://docs.fal.ai/model-apis/model-endpoints/queue
* Pricing - https://fal.ai/pricing
* Wan 2.2 A14B model page - https://fal.ai/models/fal-ai/wan/v2.2-a14b/image-to-video

fal is worth having because it resells Kling, MiniMax and PixVerse at
per-second prices with no prepaid pack, which is the cheapest no-commitment
route to several of them. Model ids are the full fal path.
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

_PRICE_SOURCE = "https://fal.ai/pricing"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _price(rates: dict[str, float], note: str, confidence: str = "verified") -> PriceSpec:
    return PriceSpec(
        mode="per_second",
        usd_per_second=rates,
        unit_name="usd",
        confidence=confidence,  # type: ignore[arg-type]
        source_url=_PRICE_SOURCE,
        checked_on="2026-10-28",
        notes=note,
    )


class FalProvider(HttpProviderAdapter):
    key = "fal"
    kind = ProviderKind.API
    base_url = "https://queue.fal.run"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Key {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            key=self.key,
            display_name="fal.ai",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="minimax/h3-max-turbo/image-to-video",
                    label="MiniMax H3 Max Turbo (via fal)",
                    resolutions=("720p",),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    price=_price(
                        {"720p": 0.015},
                        (
                            "The pricing page quotes 66.67 seconds per dollar, normalised "
                            "to \"5s at 720p\". The resolution behind that figure was not "
                            "confirmed on the model page."
                        ),
                        confidence="estimated",
                    ),
                ),
                ModelSpec(
                    id="fal-ai/wan/v2.2-a14b/image-to-video",
                    label="Wan 2.2 A14B (via fal)",
                    resolutions=("480p", "720p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=_price(
                        {"480p": 0.04, "720p": 0.08},
                        "Per second of output, counted at 16 fps.",
                    ),
                ),
                ModelSpec(
                    id="fal-ai/pixverse/v6/image-to-video",
                    label="PixVerse V6 (via fal)",
                    durations=(5, 8),
                    resolutions=("720p", "1080p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    price=_price({"720p": 0.045, "1080p": 0.090}, "Per second of output."),
                ),
                ModelSpec(
                    id="fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
                    label="Kling 2.5 Turbo Pro (via fal)",
                    durations=(5, 10),
                    resolutions=("720p", "1080p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_negative_prompt=True,
                    price=_price(
                        {"720p": 0.07, "1080p": 0.07},
                        (
                            "The pricing page quotes 14.29 seconds per dollar. Whether "
                            "1080p costs more was not confirmed, so the same rate is "
                            "shown for both and 1080p may be understated."
                        ),
                        confidence="estimated",
                    ),
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=MAX_IMAGE_BYTES,
                accepts_public_url=True,
                accepts_base64=True,
                notes="Per-model input limits are documented on each model page, not centrally.",
            ),
            required_credentials=("api_key",),
            default_concurrency=1,
            default_requests_per_minute=None,
            min_poll_interval_s=5,
            result_url_ttl_hours=None,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://docs.fal.ai/model-apis/model-endpoints/queue",
            signup_url="https://fal.ai/dashboard/keys",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "No free allowance was found on any official fal page. Credits must be "
                "bought up front (checked 2026-10-28)."
            ),
            automation_note=(
                "fal is an API product: automated use is the intended use, and no fal "
                "page read during research restricts it."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "fal retries failed runs automatically up to ten times, which means one "
                "submission can consume more than one unit of compute. Watch the first "
                "few real jobs closely.",
                "Default concurrency is 1 and the per-account cap is not published.",
                "Media URL lifetime follows your account's media-expiration setting.",
                "The per-second prices for MiniMax H3 Max Turbo and Kling 2.5 Turbo Pro "
                "are derived from \"seconds per dollar\" figures and are estimates.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        payload: dict[str, object] = {
            "image_url": data_uri(request.image_path, max_bytes=MAX_IMAGE_BYTES),
            "prompt": request.prompt,
            "resolution": request.resolution,
            "aspect_ratio": request.aspect_ratio,
        }
        if request.duration_s:
            payload["duration"] = request.duration_s
        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            payload["seed"] = request.seed

        data = await self.request_json(
            "POST", f"/{request.model_id}", json_body=payload, uncertain_on_timeout=True
        )
        request_id = data.get("request_id")
        if not request_id:
            raise self.unexpected(data, "submit response without a request_id")
        # The model path is needed to poll, and fal's status URL encodes it,
        # so both are kept in the task reference.
        return SubmitResult(
            task_ref=f"{request.model_id}|{request_id}",
            accepted=True,
            message="Queued by fal.",
        )

    @staticmethod
    def _split(task_ref: str) -> tuple[str, str]:
        model_path, _, request_id = task_ref.partition("|")
        return model_path, request_id or model_path

    async def poll(self, task_ref: str) -> TaskStatus:
        model_path, request_id = self._split(task_ref)
        # fal's queue paths drop everything after the owner and model name.
        queue_root = "/".join(model_path.split("/")[:2])

        data = await self.request_json(
            "GET", f"/{queue_root}/requests/{request_id}/status"
        )
        status = str(data.get("status") or "").upper()

        if status == "COMPLETED":
            result = await self.request_json("GET", f"/{queue_root}/requests/{request_id}")
            url = self._extract_video_url(result)
            if not url:
                return TaskStatus(
                    state="failed",
                    message="fal reported completion but returned no video URL.",
                    error_class=ErrorClass.UNKNOWN,
                    raw=result,
                )
            return TaskStatus(state="succeeded", video_url=url, progress=1.0, raw=result)

        if status in ("IN_QUEUE", "ENQUEUED"):
            position = data.get("queue_position")
            return TaskStatus(
                state="pending",
                message=f"Queued by fal at position {position}." if position is not None else "Queued by fal.",
                raw=data,
            )
        if status == "IN_PROGRESS":
            return TaskStatus(state="running", message="Generating.", raw=data)
        if status in ("FAILED", "ERROR"):
            return TaskStatus(
                state="failed",
                message=str(data.get("error") or "fal reported a failure."),
                error_class=ErrorClass.TRANSIENT,
                raw=data,
            )
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)

    @staticmethod
    def _extract_video_url(result: dict) -> str | None:
        video = result.get("video")
        if isinstance(video, dict) and video.get("url"):
            return str(video["url"])
        if isinstance(video, str):
            return video
        videos = result.get("videos")
        if isinstance(videos, list) and videos:
            first = videos[0]
            if isinstance(first, dict) and first.get("url"):
                return str(first["url"])
        return None
