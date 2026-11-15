"""Google Gemini as the script writer.

Request shape taken from the official structured-output documentation on
2026-10-29:

    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    x-goog-api-key: <key>
    {"contents": [{"parts": [{"text": ...}]}],
     "generationConfig": {"responseFormat": {"text": {
         "mimeType": "APPLICATION_JSON", "schema": {...}}}}}

The schema is enforced server-side, so the reply is a JSON string rather
than prose with a code fence around it. Parsing is still defensive: a
refusal, a truncation or a safety block all produce a reply that does not
match, and each needs a different message.

Free-tier limits are deliberately not hardcoded. Google has changed the
requests-per-day allowance on free projects more than once, and a number
baked in here would be wrong and trusted. The adapter learns the real limit
from the 429 it eventually gets.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.constants import ErrorClass
from app.providers.http import (
    DEFAULT_TIMEOUT,
    build_client,
    classify_http_error,
    sanitize_provider_message,
)
from app.providers.script.base import (
    ScriptCapabilities,
    ScriptProvider,
    ScriptProviderError,
    ScriptRequest,
    ScriptResult,
)
from app.services import scene_plan

log = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Models the structured-output docs list as supporting a response schema,
#: cheapest-first among the ones worth using for prose.
MODELS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-4-argon",
)
DEFAULT_MODEL = "gemini-3.8-flash"

#: A five-minute scene plan is long. Too low a ceiling truncates the JSON
#: mid-array, which reads as a parse failure rather than a limit.
MAX_OUTPUT_TOKENS = 32768


class GeminiScriptProvider(ScriptProvider):
    key = "gemini"

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._client: httpx.AsyncClient | None = None

    def capabilities(self) -> ScriptCapabilities:
        return ScriptCapabilities(
            key=self.key,
            display_name="Google Gemini",
            models=MODELS,
            default_model=DEFAULT_MODEL,
            required_credentials=("api_key",),
            enforces_schema=True,
            automated=True,
            docs_url="https://ai.google.dev/gemini-api/docs/generate-content/structured-output",
            signup_url="https://aistudio.google.com/apikey",
            free_tier_note=(
                "Google AI Studio issues a free-tier key. The requests-per-day "
                "allowance is set per project, is not published reliably, and has "
                "been reduced before, so this app does not display a number it "
                "cannot verify. Check your own limits in AI Studio."
            ),
            checked_on="2026-10-29",
            endpoint_verified=True,
            integration_notes=(
                "The response schema is enforced by the API, not just requested "
                "in the prompt.",
                "One script is one request, so even a very small daily allowance "
                "is enough for a video a day.",
                "Marked unverified until a real call to your key succeeds.",
            ),
        )

    # -- HTTP -------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = build_client(
                base_url=BASE_URL,
                headers={
                    "x-goog-api-key": self.credentials.get("api_key", ""),
                    "Content-Type": "application/json",
                },
                timeout=DEFAULT_TIMEOUT,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Work -------------------------------------------------------------

    async def write(self, request: ScriptRequest, *, model_id: str = "") -> ScriptResult:
        if not self.is_configured:
            raise ScriptProviderError(
                "No Gemini API key is configured. Add AVM_GEMINI_API_KEY to .env.",
                ErrorClass.AUTH,
            )

        model = model_id or DEFAULT_MODEL
        if model not in MODELS:
            raise ScriptProviderError(
                f"'{model}' is not one of the models this adapter declares.",
                ErrorClass.INVALID_INPUT,
            )

        prompt = scene_plan.build_prompt(request, motion_budget_s=request.motion_budget_s)

        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": scene_plan.scene_plan_schema(),
                    }
                },
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
            },
        }

        try:
            response = await self._http().post(
                f"/models/{model}:generateContent", json=body
            )
        except httpx.TimeoutException as exc:
            # Nothing is charged per-request here and nothing is left running,
            # so a timeout is safe to retry.
            raise ScriptProviderError(
                f"Gemini timed out: {type(exc).__name__}", ErrorClass.TRANSIENT
            ) from exc
        except httpx.TransportError as exc:
            raise ScriptProviderError(
                f"Could not reach Gemini: {sanitize_provider_message(str(exc))}",
                ErrorClass.TRANSIENT,
            ) from exc

        if not response.is_success:
            detail = sanitize_provider_message(response.text)
            error_class = classify_http_error(response.status_code, detail)
            retry_after = _retry_after(response)
            raise ScriptProviderError(
                f"Gemini returned HTTP {response.status_code}: {detail or 'no body'}",
                error_class,
                retry_after_s=retry_after,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ScriptProviderError(
                "Gemini returned a body that was not JSON.", ErrorClass.TRANSIENT
            ) from exc

        return _parse(payload, model)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse(payload: dict[str, Any], model: str) -> ScriptResult:
    """Turn a generateContent reply into a plan, or explain why not."""
    if not isinstance(payload, dict):
        raise ScriptProviderError("Gemini returned an unexpected body.", ErrorClass.TRANSIENT)

    # A prompt-level block has no candidates at all.
    feedback = payload.get("promptFeedback") or {}
    blocked = feedback.get("blockReason")
    if blocked:
        raise ScriptProviderError(
            f"Gemini refused the prompt ({blocked}). Reword the brief.",
            ErrorClass.MODERATION,
        )

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ScriptProviderError(
            "Gemini returned no candidates.", ErrorClass.TRANSIENT
        )

    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    reason = str(candidate.get("finishReason") or "").upper()
    if reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}:
        raise ScriptProviderError(
            f"Gemini stopped for {reason}. Reword the brief.", ErrorClass.MODERATION
        )
    if reason == "MAX_TOKENS":
        raise ScriptProviderError(
            "Gemini hit its output limit before finishing the plan. Ask for a "
            "shorter video or fewer scenes.",
            ErrorClass.INVALID_INPUT,
        )
    if reason == "RECITATION":
        raise ScriptProviderError(
            "Gemini stopped because the reply was reciting existing text. Retry "
            "for an original story.",
            ErrorClass.TRANSIENT,
        )

    text = _first_text(candidate)
    if not text:
        raise ScriptProviderError(
            f"Gemini returned an empty reply (finishReason {reason or 'unknown'}).",
            ErrorClass.TRANSIENT,
        )

    try:
        plan = json.loads(text)
    except ValueError as exc:
        raise ScriptProviderError(
            "Gemini's reply was not valid JSON even though a schema was supplied. "
            f"It began: {sanitize_provider_message(text, 160)}",
            ErrorClass.TRANSIENT,
        ) from exc

    if not isinstance(plan, dict):
        raise ScriptProviderError(
            "Gemini returned JSON that was not an object.", ErrorClass.TRANSIENT
        )

    scenes = plan.get("scenes")
    return ScriptResult(
        title=str(plan.get("title") or "").strip(),
        logline=str(plan.get("logline") or "").strip(),
        synopsis=str(plan.get("synopsis") or "").strip(),
        scenes=[s for s in scenes if isinstance(s, dict)] if isinstance(scenes, list) else [],
        raw=plan,
        model_id=model,
        usage=payload.get("usageMetadata") or {},
    )


def _first_text(candidate: dict[str, Any]) -> str:
    content = candidate.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "".join(chunks).strip()
