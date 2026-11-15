"""A Hugging Face Space as the fallback for borrowed GPU time.

Used when Kaggle's weekly allowance is gone. A Space is a different shape
of thing from a Kaggle notebook: there is no code to push, only an
existing app to call, so this adapter cannot run arbitrary work. It can
only call a Space the user has pointed it at.

The protocol is Gradio's documented two-step HTTP API:

* ``POST {space}/call/{api_name}`` with ``{"data": [...]}`` returns
  ``{"event_id": "..."}``
* ``GET {space}/call/{api_name}/{event_id}`` streams server-sent events
  and ends with ``event: complete`` followed by ``data: [...]``

Newer Gradio serves both under a ``/gradio_api`` prefix, so each request
tries that first and falls back to the bare path.

What this adapter cannot know is the **argument order** of whichever
Space the user names: that is defined by the Space's own author. The
``api_name`` and the argument layout are configuration, not something
guessed here, and a Space that is asleep or rate limited is reported as
such rather than retried into the ground.
"""

from __future__ import annotations

import json
import logging
import re
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
    SubmitOutcome,
)

log = logging.getLogger(__name__)

SUBMIT_TIMEOUT_S = 120
#: One poll reads the event stream for this long before reporting "still
#: running". Short enough that a request handler is never blocked.
POLL_TIMEOUT_S = 25
DOWNLOAD_TIMEOUT_S = 900

#: Tried in order. The first is current Gradio, the second older ones.
PATH_PREFIXES = ("/gradio_api/call", "/call")

_EVENT_RE = re.compile(r"^event:\s*(\S+)\s*$")
_DATA_RE = re.compile(r"^data:\s*(.*)$")


def space_url(space: str) -> str:
    """Accept ``owner/space``, a bare host, or a full URL."""
    text = (space or "").strip().rstrip("/")
    if not text:
        raise ComputeError(
            "No Hugging Face Space is configured. Set AVM_HF_SPACE_I2V or "
            "AVM_HF_SPACE_LIPSYNC to a Space such as owner/space-name.",
            ErrorClass.INVALID_INPUT,
        )
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if "/" in text:
        owner, _, name = text.partition("/")
        slug = re.sub(r"[^a-zA-Z0-9-]+", "-", f"{owner}-{name}").lower()
        return f"https://{slug}.hf.space"
    return f"https://{text}.hf.space"


def parse_event_stream(text: str) -> tuple[str, Any]:
    """Read a Gradio SSE body. Returns ``(event, payload)``.

    Gradio ends with ``complete`` or ``error``; anything else means the
    run is still going, which is reported as such rather than guessed at.
    """
    event = ""
    payload: Any = None
    for line in text.splitlines():
        matched = _EVENT_RE.match(line)
        if matched:
            event = matched.group(1)
            continue
        data = _DATA_RE.match(line)
        if data and event in {"complete", "error"}:
            raw = data.group(1).strip()
            if raw and raw != "null":
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = raw
    return event, payload


class HuggingFaceSpaceCompute(ComputeProvider):
    key = "hf_space"

    def __init__(self, credentials: dict[str, str] | None = None) -> None:
        super().__init__(credentials)
        self._client: httpx.AsyncClient | None = None
        #: Maps our ref back to the Space and endpoint it belongs to, so a
        #: poll after a restart still knows where to look.
        self._routes: dict[str, tuple[str, str]] = {}

    def capabilities(self) -> ComputeCapabilities:
        return ComputeCapabilities(
            key=self.key,
            display_name="Hugging Face Space",
            purposes=(RemotePurpose.IMAGE_TO_VIDEO, RemotePurpose.LIPSYNC),
            required_credentials=("space",),
            accelerators=(),
            free=True,
            free_tier_note=(
                "Public Spaces are free to call but are shared, frequently queued, and "
                "asleep until woken. No quota figure is published that this app can read, "
                "so remaining capacity is reported as unknown."
            ),
            docs_url="https://www.gradio.app/guides/querying-gradio-apps-with-curl",
            signup_url="https://huggingface.co/settings/tokens",
            checked_on="2026-10-29",
            endpoint_verified=True,
            live_verified=False,
            notes=(
                "You choose which Space to call. This app cannot know a Space's "
                "argument order, so the endpoint name and inputs are configuration.",
                "A free Space may be asleep, queued behind other users, or rate limited. "
                "Those are reported, not retried around.",
                "Check the Space's own licence before publishing anything it produced.",
            ),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.credentials.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_S)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _redact(self, text: str) -> str:
        token = self.credentials.get("token", "")
        return text.replace(token, "***") if token and len(token) > 3 else text

    async def submit(self, spec: ComputeJobSpec) -> SubmitOutcome:
        base = space_url(self.credentials.get("space", ""))
        api_name = self.credentials.get("api_name", "predict").lstrip("/")
        body = {"data": json.loads(spec.notebook) if spec.notebook else []}

        last_problem = ""
        for prefix in PATH_PREFIXES:
            url = f"{base}{prefix}/{api_name}"
            try:
                response = await self.client().post(
                    url, json=body, headers=self._headers()
                )
            except httpx.TimeoutException as exc:
                raise ComputeError(
                    f"The Space did not respond within {SUBMIT_TIMEOUT_S}s. It may be "
                    "asleep; open it in a browser once to wake it.",
                    ErrorClass.TRANSIENT,
                ) from exc
            except httpx.TransportError as exc:
                raise ComputeError(
                    f"Could not reach the Space: {self._redact(str(exc))}",
                    ErrorClass.TRANSIENT,
                ) from exc

            if response.status_code == 404:
                last_problem = f"No endpoint at {prefix}/{api_name}."
                continue
            self._raise_for_status(response)

            try:
                event_id = response.json().get("event_id")
            except ValueError as exc:
                raise ComputeError(
                    "The Space replied with something that is not JSON, which usually "
                    "means it served its web page instead of its API.",
                    ErrorClass.UNCERTAIN,
                ) from exc
            if not event_id:
                raise ComputeError(
                    "The Space accepted the call but returned no event id, so there is "
                    "nothing to poll.",
                    ErrorClass.UNCERTAIN,
                )
            ref = f"{base}|{prefix}/{api_name}|{event_id}"
            self._routes[ref] = (base, f"{prefix}/{api_name}")
            return SubmitOutcome(ref=ref, url=base, raw={"event_id": event_id})

        raise ComputeError(
            f"The Space has no callable endpoint named '{api_name}'. {last_problem} "
            "Check the Space's API docs for the right name.",
            ErrorClass.INVALID_INPUT,
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise ComputeError(
                "The Space refused the request. A private Space needs AVM_HF_TOKEN set.",
                ErrorClass.AUTH,
            )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise ComputeError(
                "The Space is rate limiting these requests. It is shared with everyone.",
                ErrorClass.RATE_LIMITED,
                retry_after_s=float(retry_after) if retry_after else None,
            )
        if response.status_code == 503:
            raise ComputeError(
                "The Space is starting up or is asleep. Try again shortly.",
                ErrorClass.TRANSIENT,
            )
        if response.status_code >= 400:
            raise ComputeError(
                f"The Space returned HTTP {response.status_code}: "
                f"{self._redact(response.text[:250])}",
                ErrorClass.TRANSIENT
                if response.status_code >= 500
                else ErrorClass.INVALID_INPUT,
            )

    @staticmethod
    def split_ref(ref: str) -> tuple[str, str, str]:
        parts = (ref or "").split("|")
        if len(parts) != 3:
            raise ComputeError(
                f"'{ref}' is not a Space reference.", ErrorClass.INVALID_INPUT
            )
        return parts[0], parts[1], parts[2]

    async def poll(self, ref: str) -> ComputeStatus:
        base, path, event_id = self.split_ref(ref)
        url = f"{base}{path}/{event_id}"
        try:
            response = await self.client().get(
                url, headers=self._headers(), timeout=POLL_TIMEOUT_S
            )
        except httpx.TimeoutException:
            # Gradio holds the stream open while the job runs, so a read
            # timeout is the normal "still working" signal here.
            return ComputeStatus(
                state=RemoteTaskStatus.RUNNING,
                message="The Space is still working on it.",
            )
        except httpx.TransportError as exc:
            raise ComputeError(
                f"Could not reach the Space: {self._redact(str(exc))}",
                ErrorClass.TRANSIENT,
            ) from exc

        self._raise_for_status(response)
        event, payload = parse_event_stream(response.text)

        if event == "complete":
            return ComputeStatus(
                state=RemoteTaskStatus.COMPLETED,
                message="The Space finished.",
                raw={"result": payload},
            )
        if event == "error":
            return ComputeStatus(
                state=RemoteTaskStatus.FAILED,
                message=f"The Space reported an error: {str(payload)[:300]}",
                raw={"result": payload},
            )
        return ComputeStatus(
            state=RemoteTaskStatus.RUNNING,
            message="The Space is still working on it.",
            raw={"event": event},
        )

    async def list_outputs(self, ref: str) -> list[OutputFile]:
        status = await self.poll(ref)
        if status.state != RemoteTaskStatus.COMPLETED:
            return []
        base, _, _ = self.split_ref(ref)
        return list(_files_in(status.raw.get("result"), base))

    async def download(self, file: OutputFile, destination: Path) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        written = 0
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S) as client, \
                    client.stream(
                        "GET", file.url, headers=self._headers(), follow_redirects=True
                    ) as response:
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

    async def check_auth(self) -> tuple[bool, str]:
        space = self.credentials.get("space", "")
        if not space:
            return False, "No Space is configured."
        base = space_url(space)
        try:
            response = await self.client().get(f"{base}/", timeout=30)
        except httpx.HTTPError as exc:
            return False, f"Could not reach {base}: {self._redact(str(exc))}"
        if response.status_code == 200:
            return True, f"{base} answered. Whether it exposes the right endpoint is untested."
        return False, f"{base} returned HTTP {response.status_code}."


def _files_in(result: Any, base: str) -> list[OutputFile]:
    """Pull file URLs out of whatever shape the Space returned.

    Gradio file outputs are dicts carrying ``url`` or ``path``. The exact
    nesting is up to the Space, so this walks the structure rather than
    assuming a layout.
    """
    found: list[OutputFile] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("url") or node.get("path")
            if isinstance(url, str) and url:
                if not url.startswith("http"):
                    url = f"{base}/gradio_api/file={url.lstrip('/')}"
                name = node.get("orig_name") or Path(url.split("?")[0]).name or "output"
                found.append(OutputFile(name=str(name), url=url))
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)
    return found
