"""MiniMax / Hailuo image-to-video.

Sources, all read 2026-10-28:

* Image-to-video - https://platform.minimax.io/docs/api-reference/video-generation-i2v.md
* Query task - https://platform.minimax.io/docs/api-reference/video-generation-v2-query.md
* File retrieve - https://platform.minimax.io/docs/api-reference/file-management-retrieve.md
* Pricing - https://platform.minimax.io/docs/pricing/overview.md
* Rate limits - https://platform.minimax.io/docs/guides/rate-limits.md

A finished task returns a ``file_id`` rather than a URL on the older flow, so
this adapter resolves the download link through the file-retrieve endpoint
when no URL is present.
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

MAX_IMAGE_BYTES = 30 * 1024 * 1024
_PRICE_SOURCE = "https://platform.minimax.io/docs/pricing/overview.md"


def _per_second(rates: dict[str, float], note: str) -> PriceSpec:
    return PriceSpec(
        mode="per_second",
        usd_per_second=rates,
        unit_name="usd",
        confidence="verified",
        source_url=_PRICE_SOURCE,
        checked_on="2026-10-28",
        notes=note,
    )


class MiniMaxProvider(HttpProviderAdapter):
    key = "minimax"
    kind = ProviderKind.API
    base_url = "https://api.minimax.io"

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            key=self.key,
            display_name="MiniMax (Hailuo)",
            kind=ProviderKind.API,
            models=(
                ModelSpec(
                    id="MiniMax-H3-Lite",
                    label="H3 Lite",
                    # MiniMax does not publish the accepted clip lengths for the
                    # H3 family, so none are declared and no snapping happens.
                    resolutions=("480p", "768p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    price=_per_second(
                        {"480p": 0.025, "768p": 0.04},
                        "Per second of output. The first five input images per task are free.",
                    ),
                ),
                ModelSpec(
                    id="MiniMax-H3-Highspeed",
                    label="H3 Highspeed",
                    resolutions=("768p",),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    price=_per_second({"768p": 0.06}, "Per second of output."),
                ),
                ModelSpec(
                    id="MiniMax-H3",
                    label="H3",
                    resolutions=("768p", "2k"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    price=_per_second({"768p": 0.08, "2k": 0.13}, "Per second of output."),
                ),
                ModelSpec(
                    id="MiniMax-Hailuo-2.3-Fast",
                    label="Hailuo 2.3 Fast (legacy endpoint)",
                    durations=(6,),
                    resolutions=("768p",),
                    aspect_ratios=("16:9", "9:16"),
                    price=PriceSpec(
                        mode="per_video",
                        usd_per_video={"6s@768p": 0.19},
                        unit_name="usd",
                        confidence="verified",
                        source_url=_PRICE_SOURCE,
                        checked_on="2026-10-28",
                        notes="Flat rate per 6-second clip.",
                    ),
                    notes="Uses the older /v1/video_generation endpoint.",
                ),
                ModelSpec(
                    id="MiniMax-Hailuo-2.3",
                    label="Hailuo 2.3 (legacy endpoint)",
                    durations=(6,),
                    resolutions=("768p", "1080p"),
                    aspect_ratios=("16:9", "9:16"),
                    price=PriceSpec(
                        mode="per_video",
                        usd_per_video={"6s@768p": 0.28, "6s@1080p": 0.49},
                        unit_name="usd",
                        confidence="verified",
                        source_url=_PRICE_SOURCE,
                        checked_on="2026-10-28",
                        notes="Flat rate per 6-second clip.",
                    ),
                    notes="Uses the older /v1/video_generation endpoint.",
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp", "heic"}),
                max_bytes=MAX_IMAGE_BYTES,
                min_side_px=256,
                max_side_px=5760,
                min_aspect=0.4,
                max_aspect=2.5,
                accepts_public_url=True,
                accepts_base64=True,
                notes=(
                    "H3 accepts 256-5760 px per side and up to 30 MB, with the whole "
                    "request body capped at 64 MB. The legacy endpoint caps images at "
                    "20 MB with a short side over 300 px."
                ),
            ),
            required_credentials=("api_key",),
            optional_credentials=("group_id",),
            default_concurrency=30,
            default_requests_per_minute=300,
            min_poll_interval_s=10,
            result_url_ttl_hours=None,
            supports_webhook=True,
            supports_idempotency_key=False,
            exposes_balance=False,
            docs_url="https://platform.minimax.io/docs/guides/video-generation.md",
            signup_url="https://platform.minimax.io",
            pricing_url=_PRICE_SOURCE,
            free_tier_note=(
                "No official page states a free API allowance. Some blogs mention trial "
                "credits for new accounts but the amount is unverified, so assume none. "
                "Hailuo website credits are nowhere documented as usable through the API "
                "(checked 2026-10-28)."
            ),
            automation_note=(
                "The documented API is the sanctioned route. The Hailuo consumer terms "
                "forbid using \"any robot, spider, or other automatic device\" to access "
                "the service, so the website must be driven by hand."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "The H3 family's accepted clip lengths are not published, so this "
                "adapter passes the requested duration through unchanged and will "
                "surface the provider's own rejection if it is wrong.",
                "Result URLs are described only as \"time-limited\"; a task can be "
                "re-queried for seven days to obtain a fresh link.",
                "Rate limits differ sharply by family: 300 requests a minute and 30 "
                "tasks in flight for H3, but 20 requests a minute for the Hailuo models.",
            ),
        )

    def _is_legacy(self, model_id: str) -> bool:
        return model_id.startswith("MiniMax-Hailuo")

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        image = data_uri(request.image_path, max_bytes=MAX_IMAGE_BYTES)

        if self._is_legacy(request.model_id):
            path = "/v1/video_generation"
            payload: dict[str, object] = {
                "model": request.model_id,
                "prompt": request.prompt,
                "first_frame_image": image,
                "duration": request.duration_s,
                "resolution": request.resolution.upper(),
            }
        else:
            path = "/v2/video_generation"
            payload = {
                "model": request.model_id,
                "content": [
                    {"type": "text", "text": request.prompt},
                    {"type": "image", "role": "first_frame", "image": image},
                ],
                "duration": request.duration_s,
                "resolution": request.resolution.upper(),
            }

        data = await self.request_json(
            "POST", path, json_body=payload, uncertain_on_timeout=True
        )
        self._raise_on_base_resp(data)

        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise self.unexpected(data, "submit response without a task_id")
        return SubmitResult(task_ref=str(task_id), accepted=True, message="Queued by MiniMax.")

    def _raise_on_base_resp(self, data: dict) -> None:
        """MiniMax reports failures inside a 200 response."""
        base = data.get("base_resp")
        if not isinstance(base, dict):
            return
        code = base.get("status_code")
        if code in (None, 0):
            return
        message = str(base.get("status_msg") or f"status_code {code}")
        lowered = message.lower()
        if code in (1004, 1039) or "auth" in lowered or "token" in lowered:
            error_class = ErrorClass.AUTH
        elif code == 1008 or any(w in lowered for w in ("balance", "insufficient", "credit")):
            error_class = ErrorClass.INSUFFICIENT_CREDIT
        elif code in (1026, 2013) or any(w in lowered for w in ("sensitive", "risk", "invalid")):
            error_class = ErrorClass.INVALID_INPUT
        elif code == 1002:
            error_class = ErrorClass.RATE_LIMITED
        else:
            error_class = ErrorClass.TRANSIENT
        raise ProviderError(f"MiniMax refused the request: {message}", error_class, raw=base)

    async def poll(self, task_ref: str) -> TaskStatus:
        data = await self.request_json("GET", f"/v2/query/video_generation/{task_ref}")
        self._raise_on_base_resp(data)
        status = str(data.get("status") or "").lower()

        if status in ("success", "succeeded"):
            url = await self._resolve_download_url(data)
            if not url:
                return TaskStatus(
                    state="failed",
                    message=(
                        "MiniMax reported success but neither a URL nor a retrievable "
                        "file id was present."
                    ),
                    error_class=ErrorClass.UNKNOWN,
                    raw=data,
                )
            return TaskStatus(state="succeeded", video_url=url, progress=1.0, raw=data)

        if status == "fail":
            return TaskStatus(
                state="failed",
                message=str(data.get("status_msg") or "MiniMax reported a failure."),
                error_class=ErrorClass.TRANSIENT,
                raw=data,
            )
        if status in ("queueing", "preparing", "pending"):
            return TaskStatus(state="pending", message="Queued by MiniMax.", raw=data)
        if status in ("processing", "running"):
            return TaskStatus(state="running", message="Generating.", raw=data)
        return TaskStatus(state="unknown", message=f"Unrecognised status '{status}'.", raw=data)

    async def _resolve_download_url(self, data: dict) -> str | None:
        content = data.get("content")
        if isinstance(content, dict) and content.get("url"):
            return str(content["url"])

        file_id = data.get("file_id")
        if isinstance(content, dict):
            file_id = file_id or content.get("file_id")
        if not file_id:
            return None

        params: dict[str, str] = {"file_id": str(file_id)}
        # MiniMax's file API is scoped to a group. Older accounts must pass
        # GroupId explicitly; newer ones infer it from the key, so it is sent
        # only when configured rather than failing the call for its absence.
        group_id = self.credentials.get("group_id")
        if group_id:
            params["GroupId"] = group_id

        detail = await self.request_json("GET", "/v1/files/retrieve", params=params)
        file_info = detail.get("file")
        if isinstance(file_info, dict):
            url = file_info.get("download_url") or file_info.get("url")
            if url:
                return str(url)
        return None
