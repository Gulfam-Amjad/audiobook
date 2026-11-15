"""A fake provider used for development and tests.

It exercises the full asynchronous lifecycle - submit, poll, succeed or fail -
without any network access or credit consumption, so the queue engine, the
selection logic and the download path can all be verified before a single
real API key exists.

Behaviour is driven by :class:`MockBehaviour` so a test can demand a
rate-limit, an auth failure, or a lost submit on demand.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.constants import BalanceSource, ErrorClass, ProviderKind
from app.providers.base import (
    AuthResult,
    BalanceResult,
    GenerationRequest,
    ImageConstraints,
    ModelSpec,
    PriceSpec,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderError,
    SubmitResult,
    TaskStatus,
)


@dataclass
class MockBehaviour:
    #: Seconds of simulated generation time before the task succeeds.
    generation_seconds: float = 2.0
    #: Fail the submit call with this classification.
    fail_submit_with: ErrorClass | None = None
    #: Fail the task during polling with this classification.
    fail_task_with: ErrorClass | None = None
    #: Accept the submit but report no task reference, simulating a lost
    #: response that the scheduler must reconcile.
    lose_submit_response: bool = False
    #: Video URL handed back on success. Tests point this at a local file.
    video_url: str = "https://example.invalid/mock/clip.mp4"
    balance: float | None = 1000.0
    balance_source: BalanceSource = BalanceSource.API_VERIFIED
    auth_ok: bool = True
    #: Count of submits seen, so tests can assert no duplicate charging.
    submit_count: int = 0
    #: client_token -> task_ref, which is what makes reconcile() meaningful.
    tokens: dict[str, str] = field(default_factory=dict)


class MockProvider(ProviderAdapter):
    key = "mock"
    kind = ProviderKind.MOCK

    def __init__(
        self,
        credentials: dict[str, str] | None = None,
        behaviour: MockBehaviour | None = None,
    ) -> None:
        super().__init__(credentials or {"api_key": "mock-key"})
        self.behaviour = behaviour or MockBehaviour()
        self._tasks: dict[str, float] = {}
        self._counter = 0

    def capabilities(self) -> ProviderCapabilities:
        price = PriceSpec(
            mode="per_second",
            usd_per_second={"540p": 0.0, "720p": 0.0, "1080p": 0.0},
            units_per_second={"540p": 1.0, "720p": 2.0, "1080p": 4.0},
            unit_name="mock-credits",
            usd_per_unit=0.0,
            confidence="verified",
            source_url="local://mock",
            checked_on="2026-10-28",
            notes="Costs nothing; the unit figures exist to exercise credit maths.",
        )
        return ProviderCapabilities(
            key=self.key,
            display_name="Mock Provider (development only)",
            kind=ProviderKind.MOCK,
            models=(
                ModelSpec(
                    id="mock-v1",
                    label="Mock v1",
                    durations=(5, 8, 10),
                    resolutions=("540p", "720p", "1080p"),
                    aspect_ratios=("16:9", "9:16", "1:1"),
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=price,
                ),
                ModelSpec(
                    id="mock-fast",
                    label="Mock Fast (5s only)",
                    durations=(5,),
                    resolutions=("540p", "720p"),
                    aspect_ratios=("16:9", "9:16"),
                    price=price,
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                max_bytes=20 * 1024 * 1024,
                min_side_px=256,
                max_side_px=8000,
                min_aspect=0.4,
                max_aspect=2.5,
                accepts_public_url=True,
                accepts_base64=True,
            ),
            required_credentials=("api_key",),
            default_concurrency=2,
            default_requests_per_minute=120,
            min_poll_interval_s=1,
            result_url_ttl_hours=24,
            supports_idempotency_key=True,
            exposes_balance=True,
            docs_url="local://mock",
            free_tier_note="Not a real provider. Generates nothing and costs nothing.",
            automation_note="Local fake; no terms apply.",
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Development only. Enable it to exercise the queue, selection and "
                "download paths without spending anything.",
            ),
        )

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        self.behaviour.submit_count += 1

        if self.behaviour.fail_submit_with is not None:
            raise ProviderError(
                f"Mock submit failure ({self.behaviour.fail_submit_with}).",
                self.behaviour.fail_submit_with,
                retry_after_s=1.0,
            )

        # Honour idempotency: the same token never starts a second task.
        existing = self.behaviour.tokens.get(request.client_token)
        if existing:
            return SubmitResult(task_ref=existing, accepted=True, message="Reused existing task.")

        self._counter += 1
        task_ref = f"mock-task-{self._counter}"
        self._tasks[task_ref] = time.monotonic() + self.behaviour.generation_seconds
        self.behaviour.tokens[request.client_token] = task_ref

        if self.behaviour.lose_submit_response:
            raise ProviderError(
                "Mock: connection dropped after the task was created.",
                ErrorClass.UNCERTAIN,
            )

        return SubmitResult(task_ref=task_ref, accepted=True, message="Queued.")

    async def poll(self, task_ref: str) -> TaskStatus:
        await asyncio.sleep(0)
        ready_at = self._tasks.get(task_ref)
        if ready_at is None:
            return TaskStatus(state="not_found", message=f"No mock task '{task_ref}'.")

        if self.behaviour.fail_task_with is not None:
            return TaskStatus(
                state="failed",
                message=f"Mock task failure ({self.behaviour.fail_task_with}).",
                error_class=self.behaviour.fail_task_with,
            )

        remaining = ready_at - time.monotonic()
        if remaining > 0:
            total = max(self.behaviour.generation_seconds, 0.001)
            return TaskStatus(
                state="running",
                progress=max(0.0, min(0.99, 1.0 - remaining / total)),
                message="Generating.",
            )

        return TaskStatus(
            state="succeeded",
            video_url=self.behaviour.video_url,
            progress=1.0,
            message="Done.",
            actual_cost_usd=0.0,
        )

    async def reconcile(self, client_token: str, task_ref: str | None) -> TaskStatus | None:
        known = self.behaviour.tokens.get(client_token) or task_ref
        if known is None:
            return None
        status = await self.poll(known)
        status.task_ref = known
        return status

    async def fetch_balance(self) -> BalanceResult:
        if self.behaviour.balance is None:
            return BalanceResult(source=BalanceSource.UNKNOWN, message="Mock balance unset.")
        return BalanceResult(
            source=self.behaviour.balance_source,
            balance=self.behaviour.balance,
            unit="mock-credits",
            message="Mock balance.",
        )

    async def check_auth(self) -> AuthResult:
        if not self.behaviour.auth_ok:
            return AuthResult(False, "Mock credentials rejected.")
        return AuthResult(True, "Mock provider reachable.")
