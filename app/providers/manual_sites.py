"""The ten consumer sites manual-assist supports, with sourced facts.

Everything here was read off an official page on 2026-10-28 unless the field
says otherwise. These figures drive the capacity calculator and the
per-site worksheets, so each one records where it came from and whether it
was verified - an unverified number must never be presented as a fact.

The hard finding from that research: added together these sites supply
roughly **45 seconds to 2 minutes** of free image-to-video per day, against a
target of 20 minutes. Seven of the ten prohibit browser automation outright,
which is why this app prepares worksheets and watches a folder instead of
driving the sites itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ManualSite:
    key: str
    name: str
    url: str
    #: Where the user signs up or logs in.
    signup_url: str
    #: Verbatim, sourced statement about the free allowance.
    free_allowance: str
    allowance_source_url: str
    allowance_verified: bool
    #: Conservative and optimistic seconds of video obtainable per day.
    seconds_per_day_low: float
    seconds_per_day_high: float
    #: One-off signup bonus in seconds of video, where one exists.
    signup_bonus_seconds: float = 0.0
    #: Highest resolution a free account can produce.
    max_free_resolution: str = "unknown"
    watermarked: bool | None = None
    #: How many jobs the site runs at once for a free account.
    free_concurrency: int | None = None
    #: What the terms say about automating the site, quoted.
    automation_clause: str = ""
    automation_source_url: str = ""
    #: True when automating the logged-in site would clearly breach the terms.
    automation_prohibited: bool = True
    multiple_accounts_prohibited: bool | None = None
    captcha_note: str = ""
    credit_unit: str = "credits"
    #: Cost in that unit for one clip at the free tier, when published.
    credit_cost_note: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    checked_on: str = "2026-10-28"

    @property
    def seconds_per_day_mid(self) -> float:
        return (self.seconds_per_day_low + self.seconds_per_day_high) / 2


SITES: tuple[ManualSite, ...] = (
    ManualSite(
        key="google-flow",
        name="Google Flow",
        url="https://labs.google/flow",
        signup_url="https://labs.google/flow",
        free_allowance=(
            "\"With or without a subscription, you receive 50 Google Flow credits per "
            "day.\" Credits do not roll over, and the daily refresh is triggered by your "
            "first generation."
        ),
        allowance_source_url="https://support.google.com/flow/answer/16526234",
        allowance_verified=True,
        seconds_per_day_low=40,
        seconds_per_day_high=70,
        signup_bonus_seconds=100,
        max_free_resolution="720p",
        watermarked=None,
        automation_clause=(
            "\"using automated means to access content\u2026 in violation of the "
            "machine-readable instructions\"; \"bypassing our systems or protective "
            "measures\""
        ),
        automation_source_url="https://policies.google.com/terms",
        automation_prohibited=True,
        multiple_accounts_prohibited=True,
        credit_unit="Flow credits",
        credit_cost_note=(
            "Unverified: Veo 3.1 Lite is reported at about 10 credits per clip, and an "
            "official table snippet showed a 10-second clip for 7 credits."
        ),
        notes=(
            "By far the largest free daily allowance of the ten, so it is worth doing "
            "first each day.",
            "During peak hours (roughly 2-5 PM UTC) non-subscribers \"may not be able to "
            "generate videos\" at all.",
            "The 100-credit signup bonus and the per-clip cost are unverified.",
        ),
    ),
    ManualSite(
        key="pixverse",
        name="PixVerse",
        url="https://app.pixverse.ai",
        signup_url="https://app.pixverse.ai/subscribe",
        free_allowance=(
            "Basic plan: 60 credits at signup, then 30 credits per day. \"Daily Credits "
            "renew every day at 0AM UTC and expire at the end of the day if unused.\" "
            "Bonus credits never expire."
        ),
        allowance_source_url="https://pixverse.ai/en/ai-info-page",
        allowance_verified=True,
        seconds_per_day_low=4,
        seconds_per_day_high=6,
        signup_bonus_seconds=8,
        max_free_resolution="540p",
        watermarked=True,
        free_concurrency=2,
        automation_clause=(
            "\"You shall not use any form of device, program, or algorithm, including "
            "spiders, robots, deep-links, and page-scrapes\u2026\""
        ),
        automation_source_url="https://pixverse.ai/en/terms-of-service",
        automation_prohibited=True,
        multiple_accounts_prohibited=True,
        credit_unit="credits",
        credit_cost_note=(
            "V6 image-to-video costs 7 credits per second at 540p and 5 at 360p, per the "
            "API price list; the web app's prices may differ."
        ),
        notes=(
            "Free output is capped at 540p and watermarked; watermark removal is paid.",
            "The community guidelines state \"each user may only use one account\".",
            "Cloudflare Turnstile appears on email signup (unverified).",
            "Daily credits reset at 00:00 UTC and are lost if unused, so this is the "
            "one site where a missed day is a real loss.",
        ),
        captcha_note="Cloudflare Turnstile on email signup (unverified).",
    ),
    ManualSite(
        key="vidu",
        name="Vidu",
        url="https://www.vidu.com",
        signup_url="https://s.vidu.com/pricing",
        free_allowance=(
            "\"New users receive trial credits\u2026 Daily Login: Earn credits by logging "
            "in every day.\" No amounts are published."
        ),
        allowance_source_url="https://s.vidu.com/pricing",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=10,
        signup_bonus_seconds=15,
        max_free_resolution="unknown",
        automation_clause=(
            "5.1(7) bans \"cheating, hacking, botting, boosting\u2026 that provide an "
            "unfair advantage\"; 5.3(7) bans \"creating or implementing any workaround to "
            "any\u2026 security features\""
        ),
        automation_source_url="https://www.vidu.com/terms",
        automation_prohibited=True,
        multiple_accounts_prohibited=False,
        notes=(
            "The daily-login amount is unpublished, so record what you actually receive "
            "rather than trusting any figure here.",
            "The widely repeated \"200 free videos a day\" belongs to the $99 Ultimate "
            "plan, not the free tier.",
        ),
    ),
    ManualSite(
        key="seaart",
        name="SeaArt",
        url="https://www.seaart.ai",
        signup_url="https://www.seaart.ai/mall",
        free_allowance=(
            "\"Free users receive 130 Stamina per day.\" Stamina \"resets daily\u2026 "
            "non-cumulative.\" The video cost is not published - the docs say to \"check "
            "the displayed resource cost\"."
        ),
        allowance_source_url="https://docs.seaart.ai/guide-1/getting-started/pricing-and-credits",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=10,
        max_free_resolution="unknown",
        free_concurrency=2,
        automation_clause=(
            "\"You will not use automated tools to access, interact with, or generate "
            "images and other assets through the Services.\""
        ),
        automation_source_url="https://image6.cdn2.seaart.me/static/tos0805_en.html",
        automation_prohibited=True,
        credit_unit="Stamina",
        credit_cost_note="Not published; the cost is shown in the app at generation time.",
        notes=(
            "Because the video cost is unpublished, the only way to know your real "
            "capacity here is to note the cost shown the first time you generate.",
        ),
    ),
    ManualSite(
        key="pika",
        name="Pika",
        url="https://pika.art",
        signup_url="https://pika.art/pricing",
        free_allowance=(
            "A cached copy of the pricing page showed \"80 monthly video credits\", with "
            "a 5-second 480p image-to-video costing 12 credits. Several "
            "September-October 2026 sources say the live page now shows \"0 credits / "
            "month, packs only\"."
        ),
        allowance_source_url="https://pika.art/pricing",
        allowance_verified=False,
        seconds_per_day_low=0,
        seconds_per_day_high=1,
        max_free_resolution="480p",
        automation_clause=(
            "\"use bots, scripts, hacks, or unauthorized third-party software\"; \"any "
            "robot, spider, scraper\u2026 automated or manual means to access, extract, "
            "copy, or collect data\""
        ),
        automation_source_url="https://pika.art/terms-of-service",
        automation_prohibited=True,
        multiple_accounts_prohibited=True,
        notes=(
            "The free allowance is the least certain of the ten; confirm it on the live "
            "pricing page before planning around it.",
            "The terms explicitly forbid creating multiple accounts \"to gain additional "
            "access to a free or trial Plan\" and to \"evade rate limits\".",
        ),
    ),
    ManualSite(
        key="kling",
        name="Kling",
        url="https://klingai.com",
        signup_url="https://klingai.com",
        free_allowance=(
            "Daily login credits exist only \"during the event\u2026 Kling subscribers "
            "can claim daily credits\" - subscribers only. Free users cannot get 1080p or "
            "watermark removal."
        ),
        allowance_source_url="https://kling.ai/docs/point-policy",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=0,
        max_free_resolution="720p",
        watermarked=True,
        automation_clause=(
            "3.1(h): \"use automated scripts to collect information from or otherwise "
            "interact with the Services\"; 3.1(f): \"bypass any measures we may use to "
            "prevent or restrict access\""
        ),
        automation_source_url="https://kling.ai/docs/user-policy",
        automation_prohibited=True,
        multiple_accounts_prohibited=True,
        captcha_note="Risk-based slider puzzle at signup and login (unverified).",
        notes=(
            "The widely cited \"66 free credits daily\" contradicts Kling's own credits "
            "policy and should not be planned around.",
            "Credits cannot be moved between accounts controlled by the same user.",
        ),
    ),
    ManualSite(
        key="hailuo",
        name="Hailuo (MiniMax)",
        url="https://hailuoai.video",
        signup_url="https://hailuoai.video",
        free_allowance=(
            "No recurring free allowance is published. Free users can queue 3 tasks and "
            "run 1 at a time, and downloads include \"a watermark\". A one-time welcome "
            "pack is reported at 150-200 credits that \"will expire three days after "
            "being granted\" (unverified)."
        ),
        allowance_source_url="https://hailuoai.video/doc/payment-policy.html",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=0,
        signup_bonus_seconds=30,
        max_free_resolution="768p",
        watermarked=True,
        free_concurrency=1,
        automation_clause=(
            "\"Use any robot, spider, or other automatic device, process, or means to "
            "access the Services for any purpose\""
        ),
        automation_source_url="https://hailuoai.video/doc/terms-of-service.html",
        automation_prohibited=True,
        notes=(
            "The welcome pack expires three days after it is granted, so claim it only "
            "when you are ready to use it.",
            "The frequently repeated \"100 daily credits\" claim is stale.",
        ),
    ),
    ManualSite(
        key="luma",
        name="Luma Dream Machine",
        url="https://lumalabs.ai/dream-machine",
        signup_url="https://lumalabs.ai/pricing",
        free_allowance=(
            "No free plan exists. The pricing page lists only Plus, Pro and Ultra."
        ),
        allowance_source_url="https://lumalabs.ai/pricing",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=0,
        max_free_resolution="none",
        automation_clause=(
            "(m): \"access, search, or otherwise use any portion of the Services\u2026 "
            "through the use of any engine, software, tool, agent, device, or mechanism "
            "(including spiders, robots, crawlers\u2026)\"; (h): \"circumvent any access "
            "restrictions\""
        ),
        automation_source_url="https://lumalabs.ai/legal/tos",
        automation_prohibited=True,
        notes=(
            "Third-party sites still list a watermarked free tier; that information is "
            "stale. There is nothing to claim here without paying.",
        ),
    ),
    ManualSite(
        key="krea",
        name="Krea",
        url="https://www.krea.ai",
        signup_url="https://www.krea.ai/pricing",
        free_allowance=(
            "100 compute units per day that refill daily, but video model access on the "
            "Free plan is shown as \"Limited\", and Krea's own blog says the free plan "
            "\"does not render video\"."
        ),
        allowance_source_url="https://www.krea.ai/pricing",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=0,
        max_free_resolution="none",
        automation_clause=(
            "Item 7 bans \"any processes that run or are activated while you are not "
            "logged into the Services\"; item 8 bans anything that \"crawls, scrapes, or "
            "spiders\u2026 (manual or automated means)\""
        ),
        automation_source_url="https://www.krea.ai/terms",
        automation_prohibited=False,
        credit_unit="compute units",
        notes=(
            "Automation inside a logged-in session is not explicitly addressed by the "
            "terms, which makes this ambiguous rather than clearly prohibited. This app "
            "still does not automate it.",
            "The daily units exist but reportedly cannot be spent on video, so treat the "
            "capacity as zero until you confirm otherwise.",
        ),
    ),
    ManualSite(
        key="higgsfield",
        name="Higgsfield",
        url="https://higgsfield.ai",
        signup_url="https://higgsfield.ai/pricing",
        free_allowance=(
            "Free plan: \"0 (limited use)\", images only, one video job at a time. A "
            "5-second 720p Seedance 2.0 clip costs 30 credits."
        ),
        allowance_source_url="https://higgsfield.ai/blog/higgsfield-pricing-explained",
        allowance_verified=True,
        seconds_per_day_low=0,
        seconds_per_day_high=0,
        max_free_resolution="720p",
        free_concurrency=1,
        automation_clause=(
            "5(vi) bans software used \"to 'scrape' or download data\"; 5(xiii) bans "
            "\"bypass any\u2026 technical or policy limitation\"; 16.2 bans "
            "\"circumventing Usage Limits\". Section 10.6 lets them throttle \"usage that "
            "is automated\"."
        ),
        automation_source_url="https://higgsfield.ai/terms",
        automation_prohibited=False,
        credit_unit="credits",
        credit_cost_note="30 credits for a 5-second 720p Seedance 2.0 clip.",
        notes=(
            "UI automation is not explicitly banned, only scraping - but free "
            "generations apply to the website only and automated tools \"always deduct "
            "credits\", so there is no free capacity to automate.",
            "An unverified report mentions 10 daily credits in the plan data, which "
            "would still be a third of one clip.",
        ),
    ),
)

SITES_BY_KEY: dict[str, ManualSite] = {site.key: site for site in SITES}


def get_site(key: str) -> ManualSite | None:
    return SITES_BY_KEY.get(key)


def total_seconds_per_day() -> tuple[float, float]:
    """Conservative and optimistic free seconds per day across all ten sites."""
    return (
        sum(site.seconds_per_day_low for site in SITES),
        sum(site.seconds_per_day_high for site in SITES),
    )


def total_signup_bonus_seconds() -> float:
    """One-off seconds available from signup bonuses, claimable once each."""
    return sum(site.signup_bonus_seconds for site in SITES)
