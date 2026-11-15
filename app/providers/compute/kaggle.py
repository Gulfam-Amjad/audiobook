"""Kaggle Notebooks as borrowed GPU time.

Kaggle gives signed-in accounts a weekly GPU allowance and lets the whole
push-run-collect cycle happen over HTTP, which is exactly what this
pipeline needs: no interactive session to babysit, and a run that can be
polled again after the app restarts.

Everything below was read off Kaggle's own API definition (the
``kagglesdk`` package that the official ``kaggle`` CLI is built on)
rather than guessed:

* the host that answers is ``https://www.kaggle.com``. ``api.kaggle.com``
  returned HTTP 404 for the same calls on 2026-11-01.
* ``POST /api/v1/kernels/push`` takes the whole request as a JSON body
  with **camelCase** field names
* ``GET /api/v1/kernels/status``, ``/output``, ``/quota`` take
  ``userName`` and ``kernelSlug`` as query parameters
* worker status is one of QUEUED, RUNNING, COMPLETE, ERROR,
  CANCEL_REQUESTED, CANCEL_ACKNOWLEDGED, NEW_SCRIPT
* the accelerator is chosen with ``machineShape``

That is why :attr:`ComputeCapabilities.endpoint_verified` is True while
:attr:`live_verified` is False: the shapes are right, but no call has been
made from this machine, because that needs the user's own credentials.

The weekly allowance is **not** hardcoded. ``/api/v1/kernels/quota``
reports the real figures and :meth:`quota` passes them through; if the
call fails, the report says the quota is unknown.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.constants import ErrorClass, RemotePurpose, RemoteTaskStatus
from app.providers.compute.base import (
    ComputeCapabilities,
    ComputeError,
    ComputeJobSpec,
    ComputeProvider,
    ComputeStatus,
    OutputFile,
    QuotaReport,
    SubmitOutcome,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.kaggle.com"
PUSH_PATH = "/api/v1/kernels/push"
STATUS_PATH = "/api/v1/kernels/status"
OUTPUT_PATH = "/api/v1/kernels/output"
QUOTA_PATH = "/api/v1/kernels/quota"

TIMEOUT_S = 120
DOWNLOAD_TIMEOUT_S = 900

#: Accelerator strings Kaggle's own metadata documentation lists.
ACCELERATORS: tuple[str, ...] = ("NvidiaTeslaT4", "NvidiaP100", "NvidiaL4", "None")
DEFAULT_ACCELERATOR = "NvidiaTeslaT4"

#: Kaggle's documented ceiling for a single session, used as the default
#: so a forgotten run cannot quietly eat the week's allowance.
DEFAULT_TIMEOUT_S = 3 * 3600

#: Inputs ride along inside the notebook as base64. Comfortable for a
#: handful of stills and a short WAV, and refused rather than silently
#: truncated beyond this.
MAX_INPUT_BYTES = 20 * 1024 * 1024

#: Kaggle's worker states, mapped onto ours.
_STATE_MAP = {
    "QUEUED": RemoteTaskStatus.QUEUED,
    "NEW_SCRIPT": RemoteTaskStatus.QUEUED,
    "RUNNING": RemoteTaskStatus.RUNNING,
    "COMPLETE": RemoteTaskStatus.COMPLETED,
    "ERROR": RemoteTaskStatus.FAILED,
    "CANCEL_REQUESTED": RemoteTaskStatus.CANCELLED,
    "CANCEL_ACKNOWLEDGED": RemoteTaskStatus.CANCELLED,
}

_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Kaggle slugs are the title lowercased with dashes for spaces."""
    slug = _SLUG_SAFE.sub("-", (text or "").lower()).strip("-")
    return slug[:50] or "avm-task"


def _duration_seconds(value: Any) -> float | None:
    """Kaggle serialises a duration as ``"3600s"`` or ``{"seconds": 3600}``."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        seconds = value.get("seconds")
        if seconds is None:
            return None
        try:
            nanos = float(value.get("nanos") or 0)
            return float(seconds) + nanos / 1_000_000_000
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text.removesuffix("s"))
    except ValueError:
        return None


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class KaggleCompute(ComputeProvider):
    key = "kaggle"

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._client: httpx.AsyncClient | None = None

    def capabilities(self) -> ComputeCapabilities:
        return ComputeCapabilities(
            key=self.key,
            display_name="Kaggle Notebooks",
            purposes=(RemotePurpose.IMAGE_TO_VIDEO, RemotePurpose.LIPSYNC),
            required_credentials=("username", "key"),
            accelerators=ACCELERATORS,
            default_accelerator=DEFAULT_ACCELERATOR,
            free=True,
            free_tier_note=(
                "Kaggle publishes a weekly accelerator allowance per account. This app "
                "does not hardcode the figure: it reads the real one from Kaggle's own "
                "quota endpoint and reports it as unknown if that call fails."
            ),
            docs_url="https://www.kaggle.com/docs/api",
            signup_url="https://www.kaggle.com/settings",
            checked_on="2026-10-29",
            endpoint_verified=True,
            live_verified=False,
            notes=(
                "Needs a username and API key from the Kaggle account settings page. "
                "This app never creates an account for you.",
                "Phone verification is required on Kaggle before a notebook may use a "
                "GPU or the internet. That is a one-off step you do yourself.",
                "Sessions can be interrupted. Every task carries a token so a poll after "
                "a restart resumes rather than re-runs.",
            ),
        )

    # -- plumbing ---------------------------------------------------------

    def _auth(self) -> tuple[str, str]:
        username = self.credentials.get("username", "")
        key = self.credentials.get("key", "")
        if not username or not key:
            raise ComputeError(
                "Kaggle needs a username and API key. Add them in .env as "
                "AVM_KAGGLE_USERNAME and AVM_KAGGLE_KEY.",
                ErrorClass.AUTH,
            )
        return username, key

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            username, key = self._auth()
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                auth=(username, key),
                timeout=TIMEOUT_S,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _redact(self, text: str) -> str:
        """Keep the API key out of anything that might be logged or shown."""
        key = self.credentials.get("key", "")
        username = self.credentials.get("username", "")
        for secret in (key, username):
            if secret and len(secret) > 3:
                text = text.replace(secret, "***")
        return text

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client().request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ComputeError(
                f"Kaggle did not respond within {TIMEOUT_S}s.", ErrorClass.TRANSIENT
            ) from exc
        except httpx.TransportError as exc:
            raise ComputeError(
                f"Could not reach Kaggle: {self._redact(str(exc))}", ErrorClass.TRANSIENT
            ) from exc

        if response.status_code in (401, 403):
            raise ComputeError(
                "Kaggle rejected the credentials. Check the username and API key, and "
                "that the account is phone-verified for GPU and internet access.",
                ErrorClass.AUTH,
            )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise ComputeError(
                "Kaggle is rate limiting these requests.",
                ErrorClass.RATE_LIMITED,
                retry_after_s=float(retry_after) if retry_after else None,
            )
        if response.status_code >= 500:
            raise ComputeError(
                f"Kaggle returned HTTP {response.status_code}.", ErrorClass.TRANSIENT
            )
        if response.status_code >= 400:
            raise ComputeError(
                f"Kaggle returned HTTP {response.status_code}: "
                f"{self._redact(response.text[:300])}",
                ErrorClass.INVALID_INPUT,
            )

        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise ComputeError(
                f"Kaggle returned something that is not JSON: "
                f"{self._redact(response.text[:200])}",
                ErrorClass.UNCERTAIN,
            ) from exc
        return payload if isinstance(payload, dict) else {"value": payload}

    # -- submission -------------------------------------------------------

    def build_payload(self, spec: ComputeJobSpec, username: str) -> dict[str, Any]:
        """The push body, in the camelCase Kaggle's API expects."""
        total = sum(len(item.data) for item in spec.inputs)
        if total > MAX_INPUT_BYTES:
            raise ComputeError(
                f"The inputs for this task are {total / 1_048_576:.1f} MB, over the "
                f"{MAX_INPUT_BYTES / 1_048_576:.0f} MB this app will embed in a notebook. "
                "Send fewer shots per run.",
                ErrorClass.INVALID_INPUT,
            )

        slug = slugify(spec.title)
        # Both GPU fields come from one resolved value. Asking for a T4 in
        # machineShape while enableGpu said False would be a contradiction
        # the API is free to resolve either way.
        shape = spec.accelerator or DEFAULT_ACCELERATOR
        return {
            "slug": f"{username}/{slug}",
            "newTitle": spec.title[:100],
            "text": spec.notebook,
            "language": "python",
            "kernelType": "notebook",
            "isPrivate": True,
            "enableInternet": spec.enable_internet,
            # enableGpu is deprecated in favour of machineShape, but is
            # still accepted and makes the intent explicit either way.
            "enableGpu": shape != "None",
            "machineShape": shape,
            "datasetDataSources": list(spec.datasets),
            "modelDataSources": list(spec.models),
            "competitionDataSources": [],
            "kernelDataSources": [],
            "categoryIds": [],
            "sessionTimeoutSeconds": spec.timeout_s or DEFAULT_TIMEOUT_S,
        }

    async def submit(self, spec: ComputeJobSpec) -> SubmitOutcome:
        username, _ = self._auth()
        payload = self.build_payload(spec, username)
        data = await self._request("POST", PUSH_PATH, json=payload)

        # Kaggle reports some failures in the body with a 200 status.
        error = data.get("error")
        if error:
            raise ComputeError(f"Kaggle refused the push: {error}", ErrorClass.INVALID_INPUT)
        ref = data.get("ref") or payload["slug"]
        if not ref:
            raise ComputeError(
                "Kaggle accepted the push but returned no reference, so there is "
                "nothing to poll.",
                ErrorClass.UNCERTAIN,
            )
        return SubmitOutcome(
            ref=str(ref),
            url=str(data.get("url") or ""),
            version=data.get("versionNumber"),
            raw=data,
        )

    # -- progress ---------------------------------------------------------

    @staticmethod
    def split_ref(ref: str) -> tuple[str, str]:
        owner, _, slug = (ref or "").partition("/")
        if not owner or not slug:
            raise ComputeError(
                f"'{ref}' is not a Kaggle reference. Expected owner/kernel-slug.",
                ErrorClass.INVALID_INPUT,
            )
        return owner, slug

    async def poll(self, ref: str) -> ComputeStatus:
        owner, slug = self.split_ref(ref)
        data = await self._request(
            "GET", STATUS_PATH, params={"userName": owner, "kernelSlug": slug}
        )
        raw_state = str(data.get("status") or "").upper()
        state = _STATE_MAP.get(raw_state)
        if state is None:
            # An unrecognised state is reported as uncertain rather than
            # assumed to be a failure, which would discard a good run, or
            # as running, which would claim knowledge we do not have. It
            # stays in the reconcile loop either way.
            return ComputeStatus(
                state=RemoteTaskStatus.UNCERTAIN,
                message=f"Kaggle reported an unfamiliar status: {raw_state or '(empty)'}.",
                raw=data,
            )
        message = str(data.get("failureMessage") or "")
        if state == RemoteTaskStatus.FAILED and not message:
            message = "Kaggle reported the run failed but gave no reason."
        return ComputeStatus(state=state, message=message, raw=data)

    async def list_outputs(self, ref: str) -> list[OutputFile]:
        owner, slug = self.split_ref(ref)
        data = await self._request(
            "GET", OUTPUT_PATH, params={"userName": owner, "kernelSlug": slug}
        )
        files: list[OutputFile] = []
        for entry in data.get("files") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("fileName") or entry.get("file_name")
            url = entry.get("url")
            if name and url:
                files.append(OutputFile(name=str(name), url=str(url)))
        return files

    async def logs(self, ref: str) -> str:
        owner, slug = self.split_ref(ref)
        data = await self._request(
            "GET", OUTPUT_PATH, params={"userName": owner, "kernelSlug": slug}
        )
        return self._redact(str(data.get("log") or ""))

    async def download(self, file: OutputFile, destination: Path) -> int:
        """Fetch one output. The URLs are pre-signed, so no auth is sent."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        written = 0
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S) as client, \
                    client.stream("GET", file.url, follow_redirects=True) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise ComputeError(
                        f"Downloading {file.name} failed with HTTP {response.status_code}.",
                        ErrorClass.DOWNLOAD,
                    )
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes(262_144):
                        handle.write(chunk)
                        written += len(chunk)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise ComputeError(
                f"Downloading {file.name} failed: {self._redact(str(exc))}",
                ErrorClass.DOWNLOAD,
            ) from exc

        partial.replace(destination)
        return written

    # -- quota ------------------------------------------------------------

    async def quota(self) -> QuotaReport:
        """The real weekly allowance, straight from Kaggle.

        Nothing here is remembered between runs or filled in from
        documentation: an unreadable answer is reported as unknown.
        """
        try:
            data = await self._request("GET", QUOTA_PATH)
        except ComputeError as exc:
            return QuotaReport(
                known=False,
                note=f"Kaggle's quota could not be read, so it is unknown. {exc.message}",
            )

        gpu = data.get("gpuQuota")
        if not isinstance(gpu, dict):
            return QuotaReport(
                known=False,
                note="Kaggle's reply contained no GPU quota, so it is unknown.",
            )

        allowed = _duration_seconds(gpu.get("totalTimeAllowed"))
        if allowed is None:
            return QuotaReport(
                known=False,
                note="Kaggle did not state a total allowance, so it is unknown.",
            )
        return QuotaReport(
            known=True,
            note="Reported by Kaggle for the current weekly window.",
            used_s=_duration_seconds(gpu.get("timeUsed")) or 0.0,
            reserved_s=_duration_seconds(gpu.get("timeReserved")) or 0.0,
            allowed_s=allowed,
            refresh_at=_timestamp(data.get("quotaRefreshTime")),
        )

    async def check_auth(self) -> tuple[bool, str]:
        if not self.is_configured:
            return await super().check_auth()
        try:
            report = await self.quota()
        except ComputeError as exc:
            return False, exc.message
        if report.known:
            hours = (report.remaining_s or 0.0) / 3600
            return True, f"Kaggle accepted the credentials. {hours:.1f} GPU hours left this week."
        return False, report.note


def encode_inputs(inputs: list[Any]) -> str:
    """Inputs as a base64 JSON blob for embedding in a notebook.

    Kept here rather than in the notebook templates so the size limit and
    the encoding live next to the API that has to carry them.
    """
    payload = [
        {
            "name": item.name,
            "kind": item.kind,
            "b64": base64.b64encode(item.data).decode("ascii"),
        }
        for item in inputs
    ]
    return json.dumps(payload, separators=(",", ":"))


async def wait_for(
    provider: KaggleCompute, ref: str, *, interval_s: float = 20.0, limit_s: float = 7200.0
) -> ComputeStatus:
    """Poll until the run finishes. Used by scripts, not by request handlers."""
    waited = 0.0
    status = ComputeStatus()
    while waited < limit_s:
        status = await provider.poll(ref)
        if status.finished:
            return status
        await asyncio.sleep(interval_s)
        waited += interval_s
    return ComputeStatus(
        state=RemoteTaskStatus.RUNNING,
        message=f"Still running after {limit_s / 60:.0f} minutes. Poll again later.",
        raw=status.raw,
    )
