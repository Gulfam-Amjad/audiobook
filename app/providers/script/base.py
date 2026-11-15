"""The script provider contract.

Separate from :mod:`app.providers.base` on purpose. An image-to-video
adapter and a script writer share almost nothing: one costs money per
second of output and polls a long-running task, the other returns text in
one call. Forcing them into one interface would mean a pile of fields that
are meaningless for half the implementations.

What they do share is the house rule: capabilities are declared, quotas are
never invented, and nothing is called verified until a real call succeeds.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from app.constants import ErrorClass


@dataclass(frozen=True)
class ImageHint:
    """One candidate still, as offered to the writer.

    The writer never sees pixels. It picks by filename and whatever the
    user wrote about the image, then the asset selector resolves the choice
    back to a real row.
    """

    image_id: int
    filename: str
    width: int | None = None
    height: int | None = None
    note: str = ""


@dataclass(frozen=True)
class ScriptRequest:
    """Everything a writer needs to plan one video."""

    project_name: str
    niche: str
    audience: str
    story_style: str
    language: str
    brief: str
    target_duration_s: int
    speaking_rate_wpm: int
    #: Preferred shot length. The writer may vary around it.
    scene_target_s: float
    max_scenes: int
    #: How many seconds of genuinely generated motion this run may spend.
    #: Stated to the writer so it rations animated shots itself.
    motion_budget_s: float = 0.0
    available_images: tuple[ImageHint, ...] = ()
    character_names: tuple[str, ...] = ()
    #: Problems from a previous attempt, so a retry can correct them
    #: instead of repeating the same mistake.
    previous_problems: tuple[str, ...] = ()


@dataclass
class ScriptResult:
    """A parsed reply. Not yet validated, and not yet persisted."""

    title: str
    logline: str
    synopsis: str
    scenes: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)
    model_id: str = ""
    #: Token counts when the provider reports them. Never estimated.
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptCapabilities:
    key: str
    display_name: str
    models: tuple[str, ...]
    default_model: str
    required_credentials: tuple[str, ...] = ()
    #: True when the provider enforces the schema server-side. When False
    #: the reply is only asked for in the prompt, so parsing must be
    #: defensive.
    enforces_schema: bool = False
    #: False for anything needing a human in the loop.
    automated: bool = True
    docs_url: str = ""
    signup_url: str = ""
    #: Sourced statement about the free tier. Shown verbatim.
    free_tier_note: str = ""
    checked_on: str = ""
    #: True only when the request path and payload shape were read off the
    #: provider's own documentation rather than inferred.
    endpoint_verified: bool = False
    integration_notes: tuple[str, ...] = ()


class ScriptProviderError(Exception):
    """A writer refused or failed, classified so the caller can react."""

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


class ScriptProvider(abc.ABC):
    key: str = ""

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        self.credentials = credentials or {}

    @abc.abstractmethod
    def capabilities(self) -> ScriptCapabilities:
        """Describe models and limits. Must not do any I/O."""

    @property
    def is_configured(self) -> bool:
        required = self.capabilities().required_credentials
        return all(self.credentials.get(name) for name in required)

    @abc.abstractmethod
    async def write(self, request: ScriptRequest, *, model_id: str = "") -> ScriptResult:
        """Produce a scene plan. Raise :class:`ScriptProviderError` on refusal."""

    async def check_auth(self) -> tuple[bool, str]:
        """Cheapest call that proves the credential works.

        Returns ``(ok, message)``. The default says so honestly rather than
        implying the credential was tested.
        """
        if not self.is_configured:
            missing = ", ".join(
                name
                for name in self.capabilities().required_credentials
                if not self.credentials.get(name)
            )
            return False, f"Missing credential(s): {missing}"
        return False, "This provider has no connection test implemented yet."

    async def aclose(self) -> None:
        """Release any HTTP client held by the provider."""
