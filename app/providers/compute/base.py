"""The borrowed-GPU contract.

This machine has a 930MX with 2 GB of VRAM. MuseTalk's own minimum tested
configuration is 4 GB and LatentSync 1.5 wants 8 GB, so lip-sync and
open-source image-to-video cannot run here at all. They run somewhere
else, on free GPU time the user already has access to, and the results
come back as files.

What makes that workable is that every submission is **resumable**. A
free session can be interrupted at any moment, so a task carries a client
token, its state lives in a ``remote_tasks`` row, and a poll after a
restart picks up exactly where it left off rather than paying for the
work twice.

On quotas: a provider reports what its own API says and nothing more. If
the number cannot be read, :class:`QuotaReport` says so. There is no
table of remembered limits in this package, because a limit this app
believes but cannot check is worse than admitting ignorance.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.constants import ErrorClass, RemoteTaskStatus


class ComputeError(Exception):
    """A remote backend refused or failed, classified for the caller."""

    def __init__(
        self,
        message: str,
        error_class: ErrorClass = ErrorClass.UNKNOWN,
        *,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class ComputeInput:
    """One file the notebook needs, carried to the remote machine."""

    name: str
    data: bytes
    #: What the notebook should treat this as: "image", "audio", "json".
    kind: str = "image"


@dataclass
class ComputeJobSpec:
    """One unit of borrowed-GPU work."""

    purpose: str
    title: str
    #: The notebook source, already rendered. Providers do not write code.
    notebook: str
    inputs: list[ComputeInput] = field(default_factory=list)
    #: Datasets to mount, e.g. a cache of model weights.
    datasets: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    enable_internet: bool = True
    #: Provider-specific accelerator name. Left to the caller because only
    #: the provider knows which strings its API accepts.
    accelerator: str = ""
    timeout_s: int = 0
    #: Idempotency key. Re-submitting with the same token must not start a
    #: second run of the same work.
    client_token: str = ""


@dataclass
class SubmitOutcome:
    """What came back from a submission. Never fabricated."""

    ref: str
    url: str = ""
    version: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ComputeStatus:
    state: str = RemoteTaskStatus.QUEUED
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.state in {
            RemoteTaskStatus.COMPLETED,
            RemoteTaskStatus.FAILED,
            RemoteTaskStatus.CANCELLED,
        }


@dataclass(frozen=True)
class OutputFile:
    name: str
    url: str


@dataclass
class QuotaReport:
    """What the provider says is left. ``known`` is the important field.

    When ``known`` is False every number here is meaningless and the UI
    must say the quota is unknown rather than show a zero.
    """

    known: bool = False
    note: str = ""
    used_s: float | None = None
    reserved_s: float | None = None
    allowed_s: float | None = None
    refresh_at: datetime | None = None

    @property
    def remaining_s(self) -> float | None:
        if not self.known or self.allowed_s is None:
            return None
        spent = (self.used_s or 0.0) + (self.reserved_s or 0.0)
        return max(0.0, self.allowed_s - spent)


@dataclass(frozen=True)
class ComputeCapabilities:
    key: str
    display_name: str
    purposes: tuple[str, ...]
    required_credentials: tuple[str, ...] = ()
    #: Accelerator names this backend's API actually accepts.
    accelerators: tuple[str, ...] = ()
    default_accelerator: str = ""
    #: True when using it costs nothing on the documented free tier.
    free: bool = True
    #: Sourced statement about the free tier. Shown verbatim, never summed
    #: into a number this app made up.
    free_tier_note: str = ""
    docs_url: str = ""
    signup_url: str = ""
    checked_on: str = ""
    #: True only when request paths and payload shapes were read off the
    #: provider's own API definition rather than inferred.
    endpoint_verified: bool = False
    #: True once a real call to this backend has succeeded on this
    #: machine. Providers ship with this False and nothing flips it but a
    #: genuine success.
    live_verified: bool = False
    notes: tuple[str, ...] = ()


class ComputeProvider(abc.ABC):
    key: str = ""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self.credentials = credentials or {}

    @abc.abstractmethod
    def capabilities(self) -> ComputeCapabilities:
        """Describe the backend. Must not do any I/O."""

    @property
    def is_configured(self) -> bool:
        required = self.capabilities().required_credentials
        return all(self.credentials.get(name) for name in required)

    @abc.abstractmethod
    async def submit(self, spec: ComputeJobSpec) -> SubmitOutcome:
        """Start the work. Raise :class:`ComputeError` rather than pretending."""

    @abc.abstractmethod
    async def poll(self, ref: str) -> ComputeStatus:
        """Ask how it is going. Must not block waiting for completion."""

    @abc.abstractmethod
    async def list_outputs(self, ref: str) -> list[OutputFile]:
        """What the finished run produced."""

    @abc.abstractmethod
    async def download(self, file: OutputFile, destination: Any) -> int:
        """Fetch one output file. Returns the bytes written."""

    async def quota(self) -> QuotaReport:
        """What the provider says about remaining free capacity.

        The default admits it does not know, which is the correct answer
        for any backend that does not expose the figure.
        """
        return QuotaReport(
            known=False,
            note=f"{self.capabilities().display_name} does not report a quota this app can read.",
        )

    async def check_auth(self) -> tuple[bool, str]:
        if not self.is_configured:
            missing = ", ".join(
                name
                for name in self.capabilities().required_credentials
                if not self.credentials.get(name)
            )
            return False, f"Missing credential(s): {missing}"
        return False, "This backend has no connection test implemented yet."

    async def cancel(self, ref: str) -> bool:
        """Stop a run. Returning False means cancellation is unsupported."""
        return False

    async def aclose(self) -> None:
        """Release any HTTP client held by the provider."""
