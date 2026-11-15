"""Manual-assist: the app plans, you generate, the app files the result.

This adapter deliberately makes no network calls. Seven of the ten consumer
sites it covers prohibit automated interaction in their terms, and the free
daily credits those sites give out cannot be spent through any API, so the
only legitimate way to use them is for a person to generate the clips.

What the app does instead is the part worth automating: it decides which
image goes to which site, writes a worksheet with the exact prompt and
settings, stamps each job with a short match code, watches an inbox folder,
and files whatever lands there against the right job - measuring the real
duration with FFprobe rather than trusting what was asked for.

A job routed here therefore goes to ``awaiting_manual`` and stays there until
a file appears. :meth:`submit` never pretends a clip was created.
"""

from __future__ import annotations

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
from app.providers.manual_sites import SITES, total_seconds_per_day

#: Free credits are what pays for manual-assist work, so every job costs $0
#: of real money. The unit is left as "credits" because each site counts
#: differently and the balances are tracked per account.
_FREE_PRICE = PriceSpec(
    mode="per_second",
    usd_per_second={
        "360p": 0.0,
        "480p": 0.0,
        "540p": 0.0,
        "720p": 0.0,
        "768p": 0.0,
        "1080p": 0.0,
    },
    unit_name="site credits",
    confidence="verified",
    source_url="",
    checked_on="2026-10-28",
    notes=(
        "No money changes hands: manual-assist spends the site's own free credits. "
        "Those credits are tracked per account on the Credits screen, where they are "
        "marked as figures you entered rather than balances the app verified."
    ),
)


class ManualAssistProvider(ProviderAdapter):
    key = "manual"
    kind = ProviderKind.MANUAL

    def capabilities(self) -> ProviderCapabilities:
        low, high = total_seconds_per_day()
        return ProviderCapabilities(
            key=self.key,
            display_name="Manual assist (you generate, the app files it)",
            kind=ProviderKind.MANUAL,
            models=(
                ModelSpec(
                    id="manual",
                    label="Whatever the site offers",
                    # Nothing is snapped: the settings are guidance printed on
                    # a worksheet, and the site decides what it will accept.
                    durations=(),
                    resolutions=(),
                    aspect_ratios=(),
                    supports_negative_prompt=True,
                    supports_seed=True,
                    price=_FREE_PRICE,
                ),
            ),
            image=ImageConstraints(
                formats=frozenset({"jpg", "jpeg", "png", "webp"}),
                notes=(
                    "Limits depend on whichever site you use; the worksheet records the "
                    "ones that are published."
                ),
            ),
            required_credentials=(),
            default_concurrency=len(SITES),
            default_requests_per_minute=None,
            min_poll_interval_s=3600,
            result_url_ttl_hours=None,
            supports_webhook=False,
            supports_idempotency_key=True,
            exposes_balance=False,
            docs_url="",
            signup_url="",
            pricing_url="",
            free_tier_note=(
                f"This is the only route to the sites' free daily credits, because none "
                f"of them can be spent through an API. Across all ten sites the research "
                f"found roughly {low:.0f} to {high:.0f} seconds of free video per day - "
                f"well short of 20 minutes, and most of it watermarked and at 540p or "
                f"720p (checked 2026-10-28)."
            ),
            automation_note=(
                "Nothing here is automated. Seven of the ten sites' terms prohibit using "
                "bots or scripts against their websites, so the app only prepares the "
                "work and files the results you download by hand."
            ),
            checked_on="2026-10-28",
            endpoint_verified=True,
            integration_notes=(
                "Jobs sent here wait in 'awaiting manual' until a matching file arrives "
                "in the inbox folder. They are never marked complete on their own.",
                "Each job carries a short match code. Put it in the downloaded "
                "filename and the watcher will attach the file to the right job; "
                "anything unmatched goes to a tray for you to assign.",
                "Site credit balances are whatever you enter, and are labelled as such. "
                "The app never infers that a daily reset happened.",
            ),
        )

    @property
    def is_configured(self) -> bool:
        """Always ready: there is no credential to supply."""
        return True

    def validate(self, request: GenerationRequest) -> list[str]:
        """Accept anything. The site, not this adapter, sets the limits."""
        return []

    async def submit(self, request: GenerationRequest) -> SubmitResult:
        raise ProviderError(
            (
                "Manual-assist jobs are not submitted over the network. The scheduler "
                "should have moved this job to 'awaiting manual' instead - if you are "
                "seeing this, that routing was bypassed."
            ),
            ErrorClass.INVALID_INPUT,
        )

    async def poll(self, task_ref: str) -> TaskStatus:
        return TaskStatus(
            state="pending",
            message=(
                "Waiting for you to generate this clip and drop the download into the "
                "inbox folder."
            ),
        )

    async def reconcile(self, client_token: str, task_ref: str | None) -> TaskStatus | None:
        """Nothing can be reconciled: no request was ever sent anywhere."""
        return None

    async def fetch_balance(self) -> BalanceResult:
        return BalanceResult(
            source=BalanceSource.UNKNOWN,
            message=(
                "Site credits can only be read by looking at the site. Enter what you "
                "see on the Credits screen and it will be recorded as your figure, not "
                "a verified one."
            ),
        )

    async def check_auth(self) -> AuthResult:
        return AuthResult(
            True,
            "Manual assist needs no credentials and is always available.",
            reached_provider=False,
        )
