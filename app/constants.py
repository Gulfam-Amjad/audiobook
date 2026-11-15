"""Shared enumerations.

These are plain string constants rather than SQL enums so that adding a value
never requires a table rebuild on SQLite.
"""

from __future__ import annotations

from enum import StrEnum


class ImageStatus(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    MISSING = "missing"


class JobStatus(StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    # Submitted, but the provider's response was lost. Must be reconciled
    # against the provider before it is ever retried or re-routed.
    UNCERTAIN = "uncertain"
    # Routed to a website the user drives by hand; waiting for a download.
    AWAITING_MANUAL = "awaiting_manual"


#: States from which no further automatic work happens.
TERMINAL_JOB_STATES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

#: States that occupy a provider concurrency slot.
ACTIVE_JOB_STATES = frozenset(
    {JobStatus.SUBMITTING, JobStatus.PROCESSING, JobStatus.DOWNLOADING}
)


class ErrorClass(StrEnum):
    """How a failure should be treated by the scheduler."""

    #: Worth retrying on the same provider after a backoff.
    TRANSIENT = "transient"
    #: Provider is rate limiting us; back off harder, keep the job.
    RATE_LIMITED = "rate_limited"
    #: Credentials are missing, wrong or expired. Never auto-retry.
    AUTH = "auth"
    #: Not enough credit or quota. Never auto-retry on this account.
    INSUFFICIENT_CREDIT = "insufficient_credit"
    #: Input rejected (bad dimensions, unsupported setting). Never retry.
    INVALID_INPUT = "invalid_input"
    #: Content moderation refused the request. Never retry.
    MODERATION = "moderation"
    #: Download failed but the provider task succeeded; retry the download.
    DOWNLOAD = "download"
    #: Outcome genuinely unknown. Requires reconciliation, never a blind retry.
    UNCERTAIN = "uncertain"
    #: Anything we could not classify.
    UNKNOWN = "unknown"


#: Failures that must never trigger an automatic retry or a fallback hop.
PERMANENT_ERROR_CLASSES = frozenset(
    {
        ErrorClass.AUTH,
        ErrorClass.INSUFFICIENT_CREDIT,
        ErrorClass.INVALID_INPUT,
        ErrorClass.MODERATION,
    }
)


class BalanceSource(StrEnum):
    #: Read from the provider's API during this session.
    API_VERIFIED = "api_verified"
    #: Typed in by the user. Never presented as fact.
    USER_ENTERED = "user_entered"
    #: Provider does not expose it and the user has not supplied a figure.
    UNKNOWN = "unknown"


class RefreshInterval(StrEnum):
    NONE = "none"
    DAILY = "daily"
    MONTHLY = "monthly"
    OTHER = "other"


class VerificationStatus(StrEnum):
    #: No credentials configured.
    NOT_CONFIGURED = "not_configured"
    #: Credentials present but no successful real call yet.
    UNVERIFIED = "unverified"
    #: A real call to the provider succeeded.
    VERIFIED = "verified"
    #: A real call was attempted and rejected.
    FAILED = "failed"


class ProviderKind(StrEnum):
    API = "api"
    MANUAL = "manual"
    MOCK = "mock"


class AssemblyStatus(StrEnum):
    PENDING = "pending"
    NORMALIZING = "normalizing"
    CONCATENATING = "concatenating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutputSource(StrEnum):
    API = "api"
    MANUAL = "manual"


SUPPORTED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})
SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov", ".mkv", ".m4v"})


# ---------------------------------------------------------------------------
# Production pipeline
# ---------------------------------------------------------------------------


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    SCRIPTED = "scripted"
    VOICED = "voiced"
    ANIMATING = "animating"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"


class PipelineStage(StrEnum):
    """Ordered stages. Each one writes a checkpoint so a run can resume."""

    SCRIPT = "script"
    CAST = "cast"
    SPEECH = "speech"
    ROUTING = "routing"
    MOTION = "motion"
    LIPSYNC = "lipsync"
    SUBTITLES = "subtitles"
    MIX = "mix"
    RENDER = "render"
    PUBLISH_ASSETS = "publish_assets"


#: Canonical order, used to decide what may run next.
PIPELINE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.SCRIPT,
    PipelineStage.CAST,
    PipelineStage.SPEECH,
    PipelineStage.ROUTING,
    PipelineStage.MOTION,
    PipelineStage.LIPSYNC,
    PipelineStage.SUBTITLES,
    PipelineStage.MIX,
    PipelineStage.RENDER,
    PipelineStage.PUBLISH_ASSETS,
)


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    #: Finished, but something downstream should know it was degraded.
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Waiting on the user: a credential, a manual download, an approval.
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class SceneStatus(StrEnum):
    PLANNED = "planned"
    #: Narration and dialogue audio exist, so the real duration is known.
    VOICED = "voiced"
    ROUTED = "routed"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"


class AnimationMethod(StrEnum):
    """How one scene's visual is produced.

    The distinction between the first four and :attr:`CAMERA_MOVE` is the
    whole point: only the first four create motion that was not in the
    source still. A camera move is honest editing, not generated animation.
    """

    #: Remote image-to-video API through the existing job queue.
    API_I2V = "api_i2v"
    #: A free website the user drives by hand, via manual assist.
    MANUAL_I2V = "manual_i2v"
    #: Open-source image-to-video on borrowed GPU time.
    REMOTE_I2V = "remote_i2v"
    #: Audio-driven mouth movement on a still or clip.
    LIPSYNC = "lipsync"
    #: FFmpeg push-in, pan or drift over the still. Not generated motion.
    CAMERA_MOVE = "camera_move"
    #: Held frame. Used only for deliberate beats, never to pad length.
    STILL = "still"
    #: A slice of real, openly licensed footage from a media library. Real
    #: motion, but filmed by someone, not generated.
    STOCK_FOOTAGE = "stock_footage"


#: Methods that genuinely synthesise new motion. Everything else is editing.
GENERATED_MOTION_METHODS = frozenset(
    {
        AnimationMethod.API_I2V,
        AnimationMethod.MANUAL_I2V,
        AnimationMethod.REMOTE_I2V,
        AnimationMethod.LIPSYNC,
    }
)


class ClipStatus(StrEnum):
    PLANNED = "planned"
    RENDERING = "rendering"
    #: File exists and was probed successfully.
    READY = "ready"
    FAILED = "failed"
    #: Superseded by a later attempt or a different method.
    REPLACED = "replaced"


class AudioKind(StrEnum):
    NARRATION = "narration"
    DIALOGUE = "dialogue"


class AudioStatus(StrEnum):
    PENDING = "pending"
    SYNTHESISING = "synthesising"
    READY = "ready"
    FAILED = "failed"


class RemoteBackend(StrEnum):
    KAGGLE = "kaggle"
    HF_SPACE = "hf_space"


class RemotePurpose(StrEnum):
    IMAGE_TO_VIDEO = "image_to_video"
    LIPSYNC = "lipsync"


class RemoteTaskStatus(StrEnum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Submitted but the response was lost. Reconcile, never resubmit blindly.
    UNCERTAIN = "uncertain"


class RenderStatus(StrEnum):
    PENDING = "pending"
    RENDERING = "rendering"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CommercialUse(StrEnum):
    """What a voice or model licence permits. Never guessed."""

    ALLOWED = "allowed"
    #: Attribution, share-alike or similar condition attached.
    CONDITIONAL = "conditional"
    #: Research or non-commercial only.
    RESTRICTED = "restricted"
    UNKNOWN = "unknown"
