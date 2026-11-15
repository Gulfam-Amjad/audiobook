"""Generation settings: normalisation, validation and capability matching.

A single set of user-chosen settings has to work across providers whose
models accept wildly different durations and resolutions. Rather than
pretending they are interchangeable, this module answers two questions
honestly:

* Given these settings, which providers can actually run this job?
* For a chosen provider, what is the nearest set of settings it accepts, and
  what exactly changed?

Nothing here silently rewrites a user's request. An adjustment is always
reported back so the queue can record it on the job.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.providers import registry
from app.providers.base import (
    GenerationRequest,
    ModelSpec,
    ProviderCapabilities,
)

#: Rough pixel height per resolution label, for "nearest" comparisons.
RESOLUTION_ORDER: dict[str, int] = {
    "360p": 360,
    "480p": 480,
    "540p": 540,
    "720p": 720,
    "768p": 768,
    "1080p": 1080,
    "2k": 1440,
    "4k": 2160,
}

#: Offered in the Settings screen. Only a label some adapter actually accepts
#: belongs here, because choosing one no provider supports would make every
#: job unroutable.
KNOWN_RESOLUTIONS: tuple[str, ...] = tuple(RESOLUTION_ORDER)

#: Likewise for ratios. Adapters snap to their nearest supported ratio and
#: report the change, so an unusual choice degrades rather than failing.
KNOWN_ASPECT_RATIOS: tuple[str, ...] = (
    "16:9",
    "9:16",
    "1:1",
    "4:3",
    "3:4",
    "21:9",
)


@dataclass
class JobSettings:
    """What gets stored on a job and handed to an adapter."""

    model_id: str = ""
    duration_s: int = 5
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> JobSettings:
        data = data or {}
        return cls(
            model_id=str(data.get("model_id") or ""),
            duration_s=int(data.get("duration_s") or 5),
            resolution=str(data.get("resolution") or "720p"),
            aspect_ratio=str(data.get("aspect_ratio") or "16:9"),
            seed=(int(data["seed"]) if data.get("seed") not in (None, "") else None),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class Adjustment:
    field_name: str
    requested: Any
    applied: Any
    reason: str

    def describe(self) -> str:
        return f"{self.field_name}: {self.requested} -> {self.applied} ({self.reason})"


@dataclass
class MatchResult:
    ok: bool
    settings: JobSettings
    adjustments: list[Adjustment] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def was_adjusted(self) -> bool:
        return bool(self.adjustments)

    def summary(self) -> str:
        if self.problems:
            return "; ".join(self.problems)
        if self.adjustments:
            return "; ".join(a.describe() for a in self.adjustments)
        return "Settings accepted as requested."


def _nearest_duration(requested: int, allowed: tuple[int, ...]) -> int:
    return min(allowed, key=lambda value: (abs(value - requested), value))


def _nearest_resolution(requested: str, allowed: tuple[str, ...]) -> str:
    target = RESOLUTION_ORDER.get(requested.lower(), 720)
    return min(
        allowed,
        key=lambda value: (
            abs(RESOLUTION_ORDER.get(value.lower(), 720) - target),
            RESOLUTION_ORDER.get(value.lower(), 720),
        ),
    )


def _aspect_value(label: str) -> float:
    try:
        width, _, height = label.partition(":")
        return float(width) / float(height)
    except (ValueError, ZeroDivisionError):
        return 16 / 9


def _nearest_aspect(requested: str, allowed: tuple[str, ...]) -> str:
    target = _aspect_value(requested)
    return min(allowed, key=lambda value: abs(_aspect_value(value) - target))


def pick_model(caps: ProviderCapabilities, settings: JobSettings) -> ModelSpec | None:
    """Choose the model to use, preferring the one the user named."""
    if settings.model_id:
        named = caps.model(settings.model_id)
        if named is not None:
            return named
    # Otherwise prefer a non-deprecated model that natively accepts the
    # requested duration, falling back to the provider's default.
    candidates = [m for m in caps.models if not m.deprecated]
    exact = [
        m
        for m in candidates
        if not m.durations or settings.duration_s in m.durations
    ]
    pool = exact or candidates
    if not pool:
        return caps.default_model
    # Cheapest first, so an unpinned job does not silently pick a premium model.
    def price_key(model: ModelSpec) -> float:
        price = model.price
        per_video = price.usd_per_video.get(settings.resolution)
        if per_video is not None:
            return per_video
        per_second = price.usd_per_second.get(settings.resolution)
        if per_second is not None:
            return per_second * settings.duration_s
        return float("inf")

    return min(pool, key=price_key)


def match_to_provider(
    provider_key: str, settings: JobSettings, *, allow_adjustment: bool = True
) -> MatchResult:
    """Fit settings to one provider, reporting every change or blocker."""
    caps = registry.get_capabilities(provider_key)
    if caps is None:
        return MatchResult(
            ok=False,
            settings=settings,
            problems=[f"Provider '{provider_key}' is not available in this build."],
        )

    model = pick_model(caps, settings)
    if model is None:
        return MatchResult(
            ok=False,
            settings=settings,
            problems=[f"{caps.display_name} declares no usable model."],
        )

    adjusted = JobSettings(
        model_id=model.id,
        duration_s=settings.duration_s,
        resolution=settings.resolution,
        aspect_ratio=settings.aspect_ratio,
        seed=settings.seed,
        extra=dict(settings.extra),
    )
    adjustments: list[Adjustment] = []
    problems: list[str] = []

    if model.durations and adjusted.duration_s not in model.durations:
        nearest = _nearest_duration(adjusted.duration_s, model.durations)
        if allow_adjustment:
            adjustments.append(
                Adjustment(
                    "duration",
                    f"{adjusted.duration_s}s",
                    f"{nearest}s",
                    f"{model.label} offers only {', '.join(f'{d}s' for d in model.durations)}",
                )
            )
            adjusted.duration_s = nearest
        else:
            problems.append(
                f"{model.label} does not support {adjusted.duration_s}s "
                f"(offers {', '.join(f'{d}s' for d in model.durations)})."
            )

    if model.resolutions and adjusted.resolution not in model.resolutions:
        nearest = _nearest_resolution(adjusted.resolution, model.resolutions)
        if allow_adjustment:
            adjustments.append(
                Adjustment(
                    "resolution",
                    adjusted.resolution,
                    nearest,
                    f"{model.label} offers only {', '.join(model.resolutions)}",
                )
            )
            adjusted.resolution = nearest
        else:
            problems.append(
                f"{model.label} does not support {adjusted.resolution} "
                f"(offers {', '.join(model.resolutions)})."
            )

    if model.aspect_ratios and adjusted.aspect_ratio not in model.aspect_ratios:
        nearest = _nearest_aspect(adjusted.aspect_ratio, model.aspect_ratios)
        if allow_adjustment:
            adjustments.append(
                Adjustment(
                    "aspect ratio",
                    adjusted.aspect_ratio,
                    nearest,
                    f"{model.label} offers only {', '.join(model.aspect_ratios)}",
                )
            )
            adjusted.aspect_ratio = nearest
        else:
            problems.append(
                f"{model.label} does not support {adjusted.aspect_ratio} "
                f"(offers {', '.join(model.aspect_ratios)})."
            )

    if adjusted.seed is not None and not model.supports_seed:
        adjustments.append(
            Adjustment("seed", adjusted.seed, None, f"{model.label} ignores seeds")
        )
        adjusted.seed = None

    # Drop provider-specific extras that this model does not declare, rather
    # than forwarding fields the API will reject.
    if adjusted.extra:
        permitted = set(model.extra_settings)
        removed = sorted(set(adjusted.extra) - permitted)
        for name in removed:
            adjusted.extra.pop(name, None)
        if removed:
            adjustments.append(
                Adjustment(
                    "extra settings",
                    ", ".join(removed),
                    "removed",
                    f"{model.label} does not declare them",
                )
            )

    return MatchResult(
        ok=not problems, settings=adjusted, adjustments=adjustments, problems=problems
    )


def build_request(
    *,
    job_id: int,
    client_token: str,
    image_path: str,
    prompt: str,
    negative_prompt: str | None,
    settings: JobSettings,
    provider_key: str,
) -> tuple[GenerationRequest, list[str]]:
    """Create an adapter request and run the adapter's own validation."""
    request = GenerationRequest(
        job_id=job_id,
        client_token=client_token,
        image_path=image_path,
        prompt=prompt,
        negative_prompt=negative_prompt,
        model_id=settings.model_id,
        duration_s=settings.duration_s,
        resolution=settings.resolution,
        aspect_ratio=settings.aspect_ratio,
        seed=settings.seed,
        extra=dict(settings.extra),
    )
    adapter = registry.get_adapter(provider_key)
    if adapter is None:
        return request, [f"Provider '{provider_key}' is not available."]

    problems = list(adapter.validate(request))

    # A negative prompt the model cannot use is dropped rather than refused,
    # since it is additive guidance, not the request itself.
    model = adapter.capabilities().model(settings.model_id)
    if model is not None and request.negative_prompt and not model.supports_negative_prompt:
        request.negative_prompt = None
        problems = [p for p in problems if "negative prompt" not in p]

    return request, problems


def eligible_providers(settings: JobSettings) -> dict[str, MatchResult]:
    """Match the settings against every registered provider."""
    return {
        key: match_to_provider(key, settings)
        for key in registry.all_capabilities()
    }


def describe_image_compatibility(
    provider_key: str, *, width: int | None, height: int | None, size_bytes: int, extension: str
) -> list[str]:
    """Check a source image against a provider's declared input limits."""
    caps = registry.get_capabilities(provider_key)
    if caps is None:
        return [f"Provider '{provider_key}' is not available."]

    limits = caps.image
    problems: list[str] = []
    ext = extension.lower().lstrip(".")

    if limits.formats and ext not in limits.formats:
        problems.append(
            f"{caps.display_name} accepts {', '.join(sorted(limits.formats))}, not .{ext}."
        )
    if limits.max_bytes and size_bytes > limits.max_bytes:
        problems.append(
            f"File is {size_bytes / 1_048_576:.1f} MB; "
            f"{caps.display_name} allows {limits.max_bytes / 1_048_576:.0f} MB."
        )
    if width and height:
        shortest, longest = min(width, height), max(width, height)
        if limits.min_side_px and shortest < limits.min_side_px:
            problems.append(
                f"Shortest side is {shortest}px; {caps.display_name} needs at least "
                f"{limits.min_side_px}px."
            )
        if limits.max_side_px and longest > limits.max_side_px:
            problems.append(
                f"Longest side is {longest}px; {caps.display_name} allows at most "
                f"{limits.max_side_px}px."
            )
        aspect = width / height
        if limits.min_aspect and aspect < limits.min_aspect:
            problems.append(
                f"Aspect ratio {aspect:.2f} is narrower than {caps.display_name}'s "
                f"minimum of {limits.min_aspect:.2f}."
            )
        if limits.max_aspect and aspect > limits.max_aspect:
            problems.append(
                f"Aspect ratio {aspect:.2f} is wider than {caps.display_name}'s "
                f"maximum of {limits.max_aspect:.2f}."
            )
    return problems
