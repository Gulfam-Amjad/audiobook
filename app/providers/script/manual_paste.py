"""Manual-assist script writing: you paste, the app validates.

This exists so the pipeline is usable with no API key at all. The app
builds the prompt, you run it wherever you already have access (the Gemini
web app, any other assistant), and you paste the JSON back.

It is deliberately not dressed up as automation. :attr:`automated` is
False, and :meth:`write` refuses rather than pretending to call anything.
The paste arrives through :meth:`parse` instead, driven by the UI.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.constants import ErrorClass
from app.providers.script.base import (
    ScriptCapabilities,
    ScriptProvider,
    ScriptProviderError,
    ScriptRequest,
    ScriptResult,
)

#: Models wrap JSON in a fence often enough that stripping it is kinder
#: than telling the user their paste was malformed.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class ManualPasteScriptProvider(ScriptProvider):
    key = "manual_paste"

    def capabilities(self) -> ScriptCapabilities:
        return ScriptCapabilities(
            key=self.key,
            display_name="Paste it yourself",
            models=("any",),
            default_model="any",
            required_credentials=(),
            enforces_schema=False,
            automated=False,
            free_tier_note=(
                "Costs nothing and needs no key. You run the prompt wherever you "
                "already have access and paste the reply back."
            ),
            checked_on="2026-10-29",
            endpoint_verified=True,
            integration_notes=(
                "Nothing is sent anywhere by this provider.",
                "The paste is validated exactly as strictly as an API reply.",
            ),
        )

    @property
    def is_configured(self) -> bool:
        return True

    async def write(self, request: ScriptRequest, *, model_id: str = "") -> ScriptResult:
        raise ScriptProviderError(
            "This provider does not call anything. Copy the prompt, run it "
            "yourself, and paste the JSON back.",
            ErrorClass.INVALID_INPUT,
        )

    def parse(self, pasted: str, *, model_id: str = "pasted") -> ScriptResult:
        """Turn a pasted reply into a plan, or say exactly what was wrong."""
        text = (pasted or "").strip()
        if not text:
            raise ScriptProviderError("Nothing was pasted.", ErrorClass.INVALID_INPUT)

        fenced = _FENCE_RE.match(text)
        if fenced:
            text = fenced.group("body").strip()

        # Some assistants add a sentence before the JSON. Falling back to the
        # outermost braces recovers that without accepting arbitrary prose.
        try:
            plan = json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ScriptProviderError(
                    "That paste does not contain a JSON object.", ErrorClass.INVALID_INPUT
                ) from None
            try:
                plan = json.loads(text[start : end + 1])
            except ValueError as exc:
                raise ScriptProviderError(
                    f"That paste is not valid JSON: {exc}", ErrorClass.INVALID_INPUT
                ) from exc

        if not isinstance(plan, dict):
            raise ScriptProviderError(
                "The pasted JSON is not an object.", ErrorClass.INVALID_INPUT
            )

        scenes: Any = plan.get("scenes")
        return ScriptResult(
            title=str(plan.get("title") or "").strip(),
            logline=str(plan.get("logline") or "").strip(),
            synopsis=str(plan.get("synopsis") or "").strip(),
            scenes=[s for s in scenes if isinstance(s, dict)]
            if isinstance(scenes, list)
            else [],
            raw=plan,
            model_id=model_id,
        )

    async def check_auth(self) -> tuple[bool, str]:
        return True, "Nothing to authenticate: you supply the text."
