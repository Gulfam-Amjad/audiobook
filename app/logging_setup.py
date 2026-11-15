"""Structured JSONL logging with secret redaction.

Every record is written as one JSON object per line so the diagnostics
exporter can filter and ship logs without parsing free text. Anything that
looks like a credential is replaced before it reaches a handler.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?key|secret[_-]?key|api[_-]?token|authorization)"
               r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9._\-]{6,})"),
    re.compile(r"\bsk-[A-Za-z0-9]{10,}"),
    re.compile(r"\br8_[A-Za-z0-9]{10,}"),
]

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def redact(text: str) -> str:
    """Mask credential-looking substrings in ``text``."""
    if not text:
        return text
    out = _SECRET_PATTERNS[0].sub(r"\1 ***", text)
    out = _SECRET_PATTERNS[1].sub(r"\1\2***", out)
    out = _SECRET_PATTERNS[2].sub("sk-***", out)
    out = _SECRET_PATTERNS[3].sub("r8_***", out)
    return out


def _coerce(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact(value) if isinstance(value, str) else value
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    return redact(str(value))


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _coerce(value)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactingConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact(str(record.msg))
        return super().format(record)


def configure_logging(logs_dir: Path, level: str = "INFO") -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "app.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonLineFormatter())
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(RedactingConsoleFormatter("%(levelname)-7s %(name)-28s %(message)s"))
    root.addHandler(console)

    # Uvicorn duplicates access logs through its own loggers; keep them quiet
    # at the root so the JSONL file stays readable.
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("watchdog").setLevel(logging.WARNING)
