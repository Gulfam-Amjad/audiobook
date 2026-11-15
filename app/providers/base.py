"""The provider adapter contract.

Every integration - official API, mock, or manual-assist - implements
:class:`ProviderAdapter`. The manager depends on this interface alone, so a
new provider is a new file in this package plus a registry entry.

Two rules shape this design:

1. **Capabilities are declared, not assumed.** The settings UI and the
   validator both read :class:`ProviderCapabilities`, so the app can never
   offer a duration, resolution or parameter that a provider does not accept.

2. **Uncertainty is representable.** Balances, prices and task outcomes all
   have an explicit "unknown" state. Nothing in this interface lets an
   adapter return a confident-looking number it did not actually obtain.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.constants import BalanceSource, ErrorClass, ProviderKind

Confidence = Literal["verified", "estimated", "unknown"]
PriceMode = Literal["per_second", "per_video", "per_token", "unknown"]


# ---------------------------------------------------------------------------
# Capability description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageConstraints:
    """What an adapter will accept as the source frame."""

    formats: frozenset[str] = frozenset({"jpg", "jpeg", "png", "webp"})
    max_bytes: int | None = None
    min_side_px: int | None = None
    max_side_px: int | None = None
    #: width / height, inclusive.
    min_aspect: float | None = None
    max_aspect: float | None = None
    accepts_public_url: bool = False
    accepts_base64: bool = False
    accepts_multipart_upload: bool = False
    #: Provider needs a separate upload call before generation.
    requires_preupload: bool = False
    notes: str = ""


@dataclass(frozen=True)
class PriceSpec:
    """Published price for one model, with provenance.

    ``source_url`` and ``checked_on`` are mandatory for anything marked
    ``verified`` so the provider comparison table can be audited later.
    """

    mode: PriceMode = "unknown"
    #: resolution label -> USD per output second.
    usd_per_second: dict[str, float] = field(default_factory=dict)
    #: USD for one clip, for flat-rate models. Keyed by resolution label, or
    #: by ``"{duration}s@{resolution}"`` when the provider's rate depends on
    #: clip length as well - Luma and Pika both price that way, and flattening
    #: it to a per-second figure would misquote one of the two lengths.
    usd_per_video: dict[str, float] = field(default_factory=dict)
    #: Provider's own unit (credits, units, tokens) per output second.
    units_per_second: dict[str, float] = field(default_factory=dict)
    unit_name: str = "credits"
    usd_per_unit: float | None = None
    confidence: Confidence = "unknown"
    source_url: str = ""
    checked_on: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    #: Durations in whole seconds that the provider accepts.
    durations: tuple[int, ...] = ()
    #: Resolution labels the provider uses, e.g. "540p", "720p", "1080p".
    resolutions: tuple[str, ...] = ()
    aspect_ratios: tuple[str, ...] = ()
    supports_negative_prompt: bool = False
    supports_seed: bool = False
    supports_audio: bool = False
    supports_camera_control: bool = False
    supports_end_frame: bool = False
    price: PriceSpec = field(default_factory=PriceSpec)
    #: Extra provider-specific fields the UI should render, as
    #: ``{"field": {"label": ..., "type": ..., "choices": [...]}}``.
    extra_settings: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    deprecated: bool = False


@dataclass(frozen=True)
class ProviderCapabilities:
    key: str
    display_name: str
    kind: ProviderKind
    models: tuple[ModelSpec, ...]
    image: ImageConstraints = field(default_factory=ImageConstraints)
    #: Names of the credential fields the adapter needs, for the setup UI.
    required_credentials: tuple[str, ...] = ()
    #: Fields the adapter will use if set but can work without, such as a
    #: region or group id. Listed separately so a blank one never makes the
    #: provider look unconfigured.
    optional_credentials: tuple[str, ...] = ()
    default_concurrency: int = 1
    default_requests_per_minute: int | None = None
    #: Shortest interval the provider documents for status polling.
    min_poll_interval_s: int = 10
    #: How long a returned video URL stays valid, when documented.
    result_url_ttl_hours: float | None = None
    supports_webhook: bool = False
    #: Provider honours an idempotency key, so a retried submit is safe.
    supports_idempotency_key: bool = False
    #: Provider exposes a credit balance through the API.
    exposes_balance: bool = False
    docs_url: str = ""
    signup_url: str = ""
    pricing_url: str = ""
    #: Honest, sourced statement about free credits. Shown verbatim in the UI.
    free_tier_note: str = ""
    #: What the provider's terms say about automation.
    automation_note: str = ""
    checked_on: str = ""
    #: True only when the request path and payload shape in this adapter were
    #: read off the provider's own documentation. False means the shape was
    #: inferred and must be confirmed before the adapter is trusted with money.
    endpoint_verified: bool = False
    #: Caveats the Providers screen shows verbatim. Anything the adapter is
    #: unsure about belongs here rather than in a comment nobody reads.
    integration_notes: tuple[str, ...] = ()

    def model(self, model_id: str) -> ModelSpec | None:
        for spec in self.models:
            if spec.id == model_id:
                return spec
        return None

    @property
    def default_model(self) -> ModelSpec | None:
        for spec in self.models:
            if not spec.deprecated:
                return spec
        return self.models[0] if self.models else None


# ---------------------------------------------------------------------------
# Call payloads
# ---------------------------------------------------------------------------


@dataclass
class GenerationRequest:
    """Everything an adapter needs to submit one clip."""

    job_id: int
    client_token: str
    image_path: str
    prompt: str
    negative_prompt: str | None
    model_id: str
    duration_s: int
    resolution: str
    aspect_ratio: str
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CostEstimate:
    usd: float
    units: float | None
    unit: str
    confidence: Confidence
    #: Plain-language explanation shown next to the number in the UI.
    basis: str = ""
    source_url: str = ""


@dataclass
class SubmitResult:
    #: Provider task identifier used for polling.
    task_ref: str | None
    accepted: bool
    message: str = ""
    #: Set when a provider returns the finished video synchronously.
    video_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskStatus:
    state: Literal["pending", "running", "succeeded", "failed", "unknown", "not_found"]
    video_url: str | None = None
    progress: float | None = None
    message: str = ""
    error_class: ErrorClass | None = None
    #: The provider's task id. Only needed from :meth:`ProviderAdapter.reconcile`,
    #: where it is how a job that lost its submit response learns which task it
    #: already owns - without it the job would have to be submitted again.
    task_ref: str | None = None
    #: Confirmed cost, when the provider reports what it actually charged.
    actual_cost_usd: float | None = None
    actual_units: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BalanceResult:
    source: BalanceSource
    balance: float | None = None
    unit: str = "credits"
    message: str = ""
    next_reset_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class AuthResult:
    ok: bool
    message: str
    #: True when the adapter actually reached the provider. A failure to
    #: reach it is not the same as a rejected credential.
    reached_provider: bool = True


class ProviderError(Exception):
    """Raised by adapters with a classification the scheduler can act on."""

    def __init__(
        self,
        message: str,
        error_class: ErrorClass = ErrorClass.UNKNOWN,
        *,
        retry_after_s: float | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class
        self.retry_after_s = retry_after_s
        self.raw = raw or {}


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class ProviderAdapter(abc.ABC):
    """Base class for all providers."""

    #: Stable identifier, used as the primary key in the ``providers`` table.
    key: str = ""
    kind: ProviderKind = ProviderKind.API

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self.credentials = credentials or {}

    # -- Declaration ------------------------------------------------------

    @abc.abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Describe models, limits and constraints. Must not do any I/O."""

    @property
    def is_configured(self) -> bool:
        """True when every required credential field has a value."""
        required = self.capabilities().required_credentials
        return all(self.credentials.get(name) for name in required)

    # -- Validation and costing ------------------------------------------

    def validate(self, request: GenerationRequest) -> list[str]:
        """Return human-readable reasons the request is unacceptable.

        The default implementation checks the request against the declared
        capabilities, which is sufficient for most adapters.
        """
        caps = self.capabilities()
        problems: list[str] = []

        model = caps.model(request.model_id)
        if model is None:
            available = ", ".join(m.id for m in caps.models) or "none"
            return [f"{caps.display_name} has no model '{request.model_id}'. Available: {available}"]

        if model.deprecated:
            problems.append(f"Model '{model.id}' is marked deprecated by the provider.")
        if model.durations and request.duration_s not in model.durations:
            allowed = ", ".join(f"{d}s" for d in model.durations)
            problems.append(f"{model.label} supports only {allowed}, not {request.duration_s}s.")
        if model.resolutions and request.resolution not in model.resolutions:
            allowed = ", ".join(model.resolutions)
            problems.append(
                f"{model.label} supports only {allowed}, not '{request.resolution}'."
            )
        if model.aspect_ratios and request.aspect_ratio not in model.aspect_ratios:
            allowed = ", ".join(model.aspect_ratios)
            problems.append(
                f"{model.label} supports only {allowed}, not '{request.aspect_ratio}'."
            )
        if request.negative_prompt and not model.supports_negative_prompt:
            problems.append(f"{model.label} does not accept a negative prompt.")
        if request.seed is not None and not model.supports_seed:
            problems.append(f"{model.label} does not accept a seed.")
        return problems

    def estimate_cost(self, request: GenerationRequest) -> CostEstimate:
        """Price one generation from the declared :class:`PriceSpec`."""
        caps = self.capabilities()
        model = caps.model(request.model_id)
        if model is None:
            return CostEstimate(0.0, None, "credits", "unknown", "Unknown model.")

        price = model.price
        res = request.resolution
        exact = f"{request.duration_s}s@{res}"

        if exact in price.usd_per_video:
            usd = price.usd_per_video[exact]
            basis = f"published rate of ${usd:.4f} for a {request.duration_s}s clip at {res}"
        elif price.mode == "per_video" and res in price.usd_per_video:
            usd = price.usd_per_video[res]
            basis = f"{price.unit_name or 'flat'} rate of ${usd:.4f} per clip at {res}"
        elif price.mode == "per_second" and res in price.usd_per_second:
            per_s = price.usd_per_second[res]
            usd = per_s * request.duration_s
            basis = f"${per_s:.4f}/s at {res} x {request.duration_s}s"
        else:
            return CostEstimate(
                0.0,
                None,
                price.unit_name,
                "unknown",
                f"No published price for {res} on {model.label}.",
                price.source_url,
            )

        units: float | None = None
        if res in price.units_per_second:
            units = price.units_per_second[res] * request.duration_s
        elif price.usd_per_unit:
            units = usd / price.usd_per_unit

        return CostEstimate(
            usd=round(usd, 6),
            units=round(units, 4) if units is not None else None,
            unit=price.unit_name,
            confidence=price.confidence,
            basis=basis,
            source_url=price.source_url,
        )

    # -- Execution --------------------------------------------------------

    @abc.abstractmethod
    async def submit(self, request: GenerationRequest) -> SubmitResult:
        """Start a generation. Raise :class:`ProviderError` on refusal."""

    @abc.abstractmethod
    async def poll(self, task_ref: str) -> TaskStatus:
        """Report the state of a previously submitted task."""

    async def reconcile(self, client_token: str, task_ref: str | None) -> TaskStatus | None:
        """Find out what happened to a submit whose response was lost.

        Returning ``None`` means "cannot tell" - the scheduler then leaves
        the job in ``uncertain`` for the user to resolve rather than risking
        a duplicate charge. Adapters that can list recent tasks should
        override this.
        """
        if task_ref:
            return await self.poll(task_ref)
        return None

    async def fetch_balance(self) -> BalanceResult:
        """Read remaining credit. Default: the provider does not expose it."""
        return BalanceResult(
            source=BalanceSource.UNKNOWN,
            message=f"{self.capabilities().display_name} does not expose a balance through its API.",
        )

    async def check_auth(self) -> AuthResult:
        """Cheapest possible call that proves the credentials work."""
        if not self.is_configured:
            missing = ", ".join(
                name
                for name in self.capabilities().required_credentials
                if not self.credentials.get(name)
            )
            return AuthResult(False, f"Missing credential(s): {missing}", reached_provider=False)
        return AuthResult(
            False,
            "This adapter has no connection test implemented yet.",
            reached_provider=False,
        )

    async def aclose(self) -> None:
        """Release any HTTP client held by the adapter."""
