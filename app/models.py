"""SQLAlchemy ORM models.

Design notes:
- Timestamps are stored as naive UTC. Helpers in :mod:`app.services.timeutil`
  do all conversion so reset-boundary logic stays in one place.
- Money is stored as float USD, which is adequate because every figure here
  is an *estimate* derived from provider price lists, not an accounting
  record. Estimates and confirmed charges are distinguished by a column, not
  by precision.
- Free-form provider payloads live in JSON columns so a new provider never
  needs a migration just to record its own fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.constants import (
    AssemblyStatus,
    AudioKind,
    AudioStatus,
    BalanceSource,
    ClipStatus,
    CommercialUse,
    ImageStatus,
    JobStatus,
    ProjectStatus,
    ProviderKind,
    RefreshInterval,
    RemoteTaskStatus,
    RenderStatus,
    SceneStatus,
    StageStatus,
    VerificationStatus,
)
from app.services.timeutil import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Setting(Base):
    """Runtime-editable key/value settings. Values are JSON-encoded."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Image library
# ---------------------------------------------------------------------------


class ImageFolder(Base):
    __tablename__ = "image_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), unique=True)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scan_found: Mapped[int] = mapped_column(Integer, default=0)
    last_scan_added: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    images: Mapped[list["Image"]] = relationship(back_populates="folder")


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (
        Index("ix_images_sha256", "sha256"),
        Index("ix_images_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("image_folders.id", ondelete="SET NULL"), nullable=True
    )
    path: Mapped[str] = mapped_column(String(1000), unique=True)
    filename: Mapped[str] = mapped_column(String(400))
    extension: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mtime: Mapped[float] = mapped_column(Float, default=0.0)
    thumb_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=ImageStatus.NEW)
    #: Set when another row already holds this sha256. Kept rather than
    #: deleted so the user can see what was skipped and why.
    duplicate_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    folder: Mapped[ImageFolder | None] = relationship(back_populates="images")
    jobs: Mapped[list["Job"]] = relationship(back_populates="image")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    body: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Default values for ``{placeholder}`` tokens found in ``body``.
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ImagePrompt(Base):
    """A per-image prompt override."""

    __tablename__ = "image_prompts"
    __table_args__ = (UniqueConstraint("image_id", name="uq_image_prompt_image"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"))
    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Providers and accounts
# ---------------------------------------------------------------------------


class Provider(Base):
    """A row per adapter, synced from the code registry at startup.

    The adapter in code is the source of truth for capabilities; this table
    only holds the user's decisions about it plus verification history.
    """

    __tablename__ = "providers"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default=ProviderKind.API)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    docs_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    signup_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(20), default=VerificationStatus.NOT_CONFIGURED
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    accounts: Mapped[list["Account"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )


class Account(Base):
    """One configured identity on a provider."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("provider_key", "label", name="uq_account_provider_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_key: Mapped[str] = mapped_column(
        ForeignKey("providers.key", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    #: Name of the env var holding this account's key, when it is not the
    #: provider default. Never the key itself.
    credential_env_var: Mapped[str | None] = mapped_column(String(100), nullable=True)
    credential_status: Mapped[str] = mapped_column(
        String(20), default=VerificationStatus.NOT_CONFIGURED
    )
    auth_status: Mapped[str] = mapped_column(String(20), default=VerificationStatus.UNVERIFIED)
    last_auth_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_auth_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Overrides the adapter default when the user knows their real tier.
    concurrency_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_job_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: True only when the user has explicitly authorised spending money here.
    paid_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="accounts")
    entitlements: Mapped[list["Entitlement"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(back_populates="account")


class Entitlement(Base):
    """A credit balance or quota for one account.

    An account may have several: a one-time trial grant, a recurring daily
    allowance, and a paid balance are tracked separately because they expire
    and reset differently.
    """

    __tablename__ = "entitlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), default="default")
    #: Free text because providers count in credits, units, tokens, seconds
    #: and neurons, and conflating them produces nonsense totals.
    unit: Mapped[str] = mapped_column(String(40), default="credits")
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default=BalanceSource.UNKNOWN)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    refresh_interval: Mapped[str] = mapped_column(String(20), default=RefreshInterval.NONE)
    #: Only ever set from a provider statement or a user entry. The app does
    #: not infer that a reset happened just because the clock passed midnight.
    next_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: USD value of one unit, when the provider publishes it.
    unit_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    account: Mapped[Account] = relationship(back_populates="entitlements")


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="batch")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_provider_status", "provider_key", "status"),
        Index("ix_jobs_next_attempt", "next_attempt_at"),
        UniqueConstraint("client_token", name="uq_jobs_client_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id", ondelete="CASCADE"))
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("batches.id", ondelete="SET NULL"), nullable=True
    )

    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Validated against the chosen adapter's capabilities before submit.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    #: Null means "let the selection engine decide at submit time".
    provider_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    #: Set when the user pinned a provider by hand; the engine must not move it.
    provider_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(24), default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, default=100)

    #: Stable idempotency key generated once, reused across every retry so a
    #: provider that supports it can collapse duplicate submissions.
    client_token: Mapped[str] = mapped_column(String(64))
    #: Provider-side task id, once known.
    task_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Short human-facing code used to match manual downloads back to a job.
    manual_token: Mapped[str | None] = mapped_column(String(32), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    download_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    poll_count: Mapped[int] = mapped_column(Integer, default=0)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(30), nullable=True)
    #: Providers already ruled out for this job, so fallback does not loop.
    excluded_providers: Mapped[list[Any]] = mapped_column(JSON, default=list)

    cost_estimate_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_estimate_units: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_estimate_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    image: Mapped[Image] = relationship(back_populates="jobs")
    account: Mapped[Account | None] = relationship(back_populates="jobs")
    batch: Mapped[Batch | None] = relationship(back_populates="jobs")
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    output: Mapped["Output | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    event: Mapped[str] = mapped_column(String(60))
    #: Human-readable, already redacted and sanitised for display.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# Outputs and assembly
# ---------------------------------------------------------------------------


class Output(Base):
    __tablename__ = "outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, unique=True
    )
    file_path: Mapped[str] = mapped_column(String(1000), unique=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="api")
    provider_key: Mapped[str | None] = mapped_column(String(50), nullable=True)

    #: Probed with ffprobe where available; null when FFmpeg is not installed.
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(40), nullable=True)
    container: Mapped[str | None] = mapped_column(String(40), nullable=True)
    has_audio: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    probe_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    downloaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    job: Mapped[Job | None] = relationship(back_populates="output")


class InboxItem(Base):
    """A file dropped into the manual-assist inbox awaiting assignment."""

    __tablename__ = "inbox_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_path: Mapped[str] = mapped_column(String(1000), unique=True)
    site_hint: Mapped[str | None] = mapped_column(String(50), nullable=True)
    detected_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="unmatched")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Assembly(Base):
    __tablename__ = "assemblies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=AssemblyStatus.PENDING)
    #: Ordered list of output ids.
    clip_order: Mapped[list[Any]] = mapped_column(JSON, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: Sum of the source clips' real durations - the AI-generated motion.
    source_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Duration of the rendered file, which transitions can shorten.
    output_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------


class SpendEntry(Base):
    """Append-only record of estimated and confirmed spending."""

    __tablename__ = "spend_entries"
    __table_args__ = (Index("ix_spend_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    provider_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    units: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: "estimate" until a provider confirms an actual charge.
    kind: Mapped[str] = mapped_column(String(20), default="estimate")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Production pipeline
#
# A Project is one video. Everything below hangs off it. The split between
# Scene (the plan), AudioSegment (measured speech) and SceneClip (a rendered
# attempt) exists so that a single bad scene can be re-made without
# invalidating the script, the narration, or the clips either side of it.
# ---------------------------------------------------------------------------


class VoiceProfile(Base):
    """A reusable named voice.

    Licence fields are recorded per voice, not per engine, because Piper's
    engine licence and its individual voice licences differ: some voices
    descend from research-only datasets and cannot be published.
    """

    __tablename__ = "voice_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    engine: Mapped[str] = mapped_column(String(40), default="piper")
    #: Absolute path to the .onnx voice. Null until the user installs it.
    model_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    config_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="en_US")
    speaker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Piper synthesis knobs. Stored so a voice sounds identical every run.
    length_scale: Mapped[float] = mapped_column(Float, default=1.0)
    noise_scale: Mapped[float] = mapped_column(Float, default=0.667)
    noise_w: Mapped[float] = mapped_column(Float, default=0.8)
    sentence_silence_s: Mapped[float] = mapped_column(Float, default=0.2)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: How fast this voice actually speaks, worked out from lines it has
    #: already recorded. Null until enough have been measured; never a
    #: figure taken from the model card, which does not state one.
    measured_wpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    measured_chars_per_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: How many lines that measurement is based on.
    measured_segments: Mapped[int] = mapped_column(Integer, default=0)
    licence: Mapped[str | None] = mapped_column(String(200), nullable=True)
    licence_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    commercial_use: Mapped[str] = mapped_column(String(20), default=CommercialUse.UNKNOWN)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    installed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Project(Base):
    """One video, from idea to MP4."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.DRAFT)

    # -- Editorial settings ------------------------------------------------
    niche: Mapped[str] = mapped_column(String(200), default="")
    audience: Mapped[str] = mapped_column(String(200), default="")
    story_style: Mapped[str] = mapped_column(String(200), default="")
    language: Mapped[str] = mapped_column(String(16), default="en")
    brief: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Format ------------------------------------------------------------
    target_duration_s: Mapped[int] = mapped_column(Integer, default=300)
    aspect_ratio: Mapped[str] = mapped_column(String(12), default="16:9")
    resolution: Mapped[str] = mapped_column(String(12), default="720p")
    fps: Mapped[int] = mapped_column(Integer, default=24)

    # -- Voice and sound ---------------------------------------------------
    narrator_voice_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True
    )
    #: Words per minute used to turn a draft script into a time estimate
    #: before any audio exists. Replaced by measured durations later.
    speaking_rate_wpm: Mapped[int] = mapped_column(Integer, default=150)
    music_preference: Mapped[str] = mapped_column(String(200), default="")
    #: Path to a music bed the user supplied. Never auto-downloaded.
    music_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    music_gain_db: Mapped[float] = mapped_column(Float, default=-18.0)

    # -- Subtitles ---------------------------------------------------------
    subtitles_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    subtitle_style: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # -- Source imagery ----------------------------------------------------
    image_folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("image_folders.id", ondelete="SET NULL"), nullable=True
    )

    #: Caps how much borrowed GPU time and how many website credits one
    #: project may consume, so a single run cannot drain the day's budget.
    max_generated_motion_s: Mapped[float] = mapped_column(Float, default=120.0)

    #: Whether the stills in this project came from an AI image model. The
    #: app cannot tell by looking, so this is only ever what you have told
    #: it. Null means you have not said, and the publishing disclosure then
    #: makes no claim either way rather than guessing one.
    stills_ai_generated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    #: The finishing pass: transitions, grade, cards, word captions, sound
    #: design. Empty means the plain cut-and-mix render of earlier films.
    production: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    narrator_voice: Mapped[VoiceProfile | None] = relationship()
    characters: Mapped[list["Character"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    scripts: Mapped[list["Script"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    checkpoints: Mapped[list["StageCheckpoint"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Character(Base):
    """A recurring person in one project, pinned to a reference still."""

    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_character_project_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The still that defines this character's appearance, for consistency.
    reference_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    voice_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True
    )
    appearance_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="characters")
    reference_image: Mapped[Image | None] = relationship()
    voice_profile: Mapped[VoiceProfile | None] = relationship()


class Script(Base):
    """One generated draft. Kept versioned so a regeneration is not a loss."""

    __tablename__ = "scripts"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_script_project_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(300), default="")
    logline: Mapped[str | None] = mapped_column(Text, nullable=True)
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The provider's reply exactly as parsed, before any of our edits.
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    word_count: Mapped[int] = mapped_column(Integer, default=0)
    #: From word count and the project's speaking rate; not measured audio.
    estimated_duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    #: Problems found by the validator. A script with errors is never used.
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Only an approved script may consume generation credits.
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="scripts")
    scenes: Mapped[list["Scene"]] = relationship(
        back_populates="script", cascade="all, delete-orphan", order_by="Scene.order_index"
    )


class Scene(Base):
    """One shot: what happens, who speaks, which still, and how it moves."""

    __tablename__ = "scenes"
    __table_args__ = (
        UniqueConstraint("script_id", "order_index", name="uq_scene_script_order"),
        Index("ix_scenes_project", "project_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    #: Stable id from the script, e.g. "s04". Survives re-ordering.
    scene_key: Mapped[str] = mapped_column(String(40), default="")

    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_duration_s: Mapped[float] = mapped_column(Float, default=8.0)

    narration: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``[{"character": "Mara", "line": "..."}]`` in delivery order.
    dialogue: Mapped[list[Any]] = mapped_column(JSON, default=list)
    character_names: Mapped[list[Any]] = mapped_column(JSON, default=list)

    image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )

    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    camera: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[str | None] = mapped_column(Text, nullable=True)
    sfx_cue: Mapped[str | None] = mapped_column(Text, nullable=True)
    transition: Mapped[str | None] = mapped_column(String(40), nullable=True)

    #: What the script asked for.
    preferred_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: What the router actually chose, given real availability.
    assigned_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: Ordered alternatives if the assigned method fails.
    fallback_methods: Mapped[list[Any]] = mapped_column(JSON, default=list)
    #: Prompt handed to an image-to-video provider, when one is used.
    motion_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The director's fields for this shot after defaults were filled:
    #: beat, cards, transition out, sound cues, music mood, held pause.
    direction: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default=SceneStatus.PLANNED)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Sum of this scene's measured audio. Null until speech is synthesised.
    measured_audio_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    script: Mapped[Script] = relationship(back_populates="scenes")
    image: Mapped[Image | None] = relationship()
    audio_segments: Mapped[list["AudioSegment"]] = relationship(
        back_populates="scene",
        cascade="all, delete-orphan",
        order_by="AudioSegment.order_index",
    )
    clips: Mapped[list["SceneClip"]] = relationship(
        back_populates="scene", cascade="all, delete-orphan"
    )


class AudioSegment(Base):
    """One synthesised utterance, and the subtitle cue that matches it.

    One row per sentence. That granularity is what makes subtitle timing
    measured rather than estimated: each file's real duration comes from
    ffprobe, so cues never drift.
    """

    __tablename__ = "audio_segments"
    __table_args__ = (Index("ix_audio_segments_scene", "scene_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    kind: Mapped[str] = mapped_column(String(16), default=AudioKind.NARRATION)
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    voice_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text)

    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True, unique=True)
    #: Measured with ffprobe. Never derived from word count.
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Hash of text plus voice settings. Lets an unchanged line be reused.
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=AudioStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    scene: Mapped[Scene] = relationship(back_populates="audio_segments")
    character: Mapped[Character | None] = relationship()
    voice_profile: Mapped[VoiceProfile | None] = relationship()


class RemoteTask(Base):
    """A unit of work sent to borrowed GPU time (Kaggle, a HF Space)."""

    __tablename__ = "remote_tasks"
    __table_args__ = (
        UniqueConstraint("client_token", name="uq_remote_task_client_token"),
        Index("ix_remote_tasks_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    scene_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True
    )
    backend: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(24))
    #: Kernel slug or Space id.
    ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=RemoteTaskStatus.PENDING)
    #: Reused across retries so a resubmit cannot duplicate work.
    client_token: Mapped[str] = mapped_column(String(64))
    #: What was sent: scene ids, model id, parameters. Never credentials.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Files the run produced, as ``[{"name": ..., "path": ...}]``.
    artifacts: Mapped[list[Any]] = mapped_column(JSON, default=list)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: Measured from submit to finish; the honest cost against a GPU quota.
    gpu_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(30), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SceneClip(Base):
    """One rendered attempt at a scene's visual.

    A scene may accumulate several: a camera move made immediately, then a
    generated clip that replaces it once borrowed GPU time comes free. Only
    one is ``selected`` at a time, which is how a single scene is swapped
    without touching the rest of the edit.
    """

    __tablename__ = "scene_clips"
    __table_args__ = (Index("ix_scene_clips_scene", "scene_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))

    method: Mapped[str] = mapped_column(String(24))
    provider_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: Set when the clip came through the image-to-video job queue.
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    remote_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("remote_tasks.id", ondelete="SET NULL"), nullable=True
    )
    output_id: Mapped[int | None] = mapped_column(
        ForeignKey("outputs.id", ondelete="SET NULL"), nullable=True
    )
    source_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )

    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: True only for methods in ``GENERATED_MOTION_METHODS``. Denormalised so
    #: the motion total can be summed without re-deriving it from strings.
    is_generated_motion: Mapped[bool] = mapped_column(Boolean, default=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(20), default=ClipStatus.PLANNED)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    scene: Mapped[Scene] = relationship(back_populates="clips")


class Render(Base):
    """A final assembly attempt for a project."""

    __tablename__ = "renders"
    __table_args__ = (Index("ix_renders_project", "project_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default=RenderStatus.PENDING)

    output_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    subtitle_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    report_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    #: Length of the finished file.
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: How much of that was genuinely generated motion. Always reported
    #: beside the total so the two are never confused.
    generated_motion_s: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Title, description, keywords and disclosure text, once generated.
    publish_assets: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    progress: Mapped[float] = mapped_column(Float, default=0.0)
    stage_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StageCheckpoint(Base):
    """Where a project got to in the pipeline, and why it stopped.

    One row per stage per project, so an interrupted run resumes from the
    last completed stage instead of starting over and re-spending credits.
    """

    __tablename__ = "stage_checkpoints"
    __table_args__ = (
        UniqueConstraint("project_id", "stage", name="uq_checkpoint_project_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(20), default=StageStatus.PENDING)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Enough state to resume: ids already done, cursors, partial results.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Set when the stage needs the user, with the exact action required.
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="checkpoints")
