"""Jinja environment and the filters the templates rely on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from app import __version__
from app.constants import BalanceSource, JobStatus, VerificationStatus
from app.services.timeutil import format_duration, humanize_delta

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _filesize(num: int | None) -> str:
    if not num:
        return "0 B"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _usd(amount: float | None) -> str:
    if amount is None:
        return "unknown"
    if amount == 0:
        return "$0.00"
    if abs(amount) < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def _number(value: float | None, places: int = 0) -> str:
    if value is None:
        return "unknown"
    return f"{value:,.{places}f}"


#: Maps a status to a CSS class so badge colouring lives in one place.
_STATUS_TONE: dict[str, str] = {
    JobStatus.QUEUED: "neutral",
    JobStatus.SUBMITTING: "busy",
    JobStatus.PROCESSING: "busy",
    JobStatus.DOWNLOADING: "busy",
    JobStatus.COMPLETED: "good",
    JobStatus.FAILED: "bad",
    JobStatus.CANCELLED: "neutral",
    JobStatus.PAUSED: "warn",
    JobStatus.UNCERTAIN: "warn",
    JobStatus.AWAITING_MANUAL: "info",
    VerificationStatus.VERIFIED: "good",
    VerificationStatus.UNVERIFIED: "warn",
    VerificationStatus.NOT_CONFIGURED: "neutral",
    VerificationStatus.FAILED: "bad",
    BalanceSource.API_VERIFIED: "good",
    BalanceSource.USER_ENTERED: "warn",
    BalanceSource.UNKNOWN: "neutral",
    "new": "neutral",
    "missing": "bad",
    "ok": "good",
    "degraded": "warn",
    "unmatched": "warn",
    "matched": "good",
}


def _tone(status: str | None) -> str:
    return _STATUS_TONE.get(str(status or ""), "neutral")


def _balance_label(source: str | None, balance: float | None, unit: str = "") -> str:
    """Render a balance so a guess can never look like a fact."""
    if source == BalanceSource.UNKNOWN or balance is None:
        return "unknown"
    rendered = f"{balance:,.0f}" if float(balance).is_integer() else f"{balance:,.2f}"
    suffix = f" {unit}" if unit else ""
    if source == BalanceSource.USER_ENTERED:
        return f"{rendered}{suffix} (you entered)"
    return f"{rendered}{suffix}"


def _pretty_status(status: str | None) -> str:
    return str(status or "").replace("_", " ")


def configure_templates(extra_globals: dict[str, Any] | None = None) -> None:
    env = templates.env
    env.filters["filesize"] = _filesize
    env.filters["usd"] = _usd
    env.filters["number"] = _number
    env.filters["tone"] = _tone
    env.filters["duration"] = format_duration
    env.filters["when"] = humanize_delta
    env.filters["pretty_status"] = _pretty_status
    env.globals["app_version"] = __version__
    env.globals["balance_label"] = _balance_label
    if extra_globals:
        env.globals.update(extra_globals)


configure_templates()
