"""Piper discovery, installation, and the voice catalogue.

Piper is the narration engine. It is a standalone native executable, which
is the whole reason it was chosen: this machine runs Python 3.14, for which
no torch or onnxruntime wheel exists, so a pip-installed TTS stack is not
an option. ``piper.exe`` carries its own runtime and does not care.

Everything lands on D: because C: has under five gigabytes free and a voice
is roughly sixty megabytes.

Nothing here is ever run automatically. Installation downloads public
files from a pinned release and a public model repository, and only when
the user presses the button.

On licences: a voice's licence is read from its own ``MODEL_CARD`` at
install time and stored verbatim. This module does not ship a table of
what it believes each voice permits. :func:`classify_licence` recognises a
short list of unambiguous licence strings and answers "unknown" for
everything else, which keeps an unrecognised voice out of a published
video rather than guessing in its favour.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import get_settings
from app.constants import CommercialUse

log = logging.getLogger(__name__)

RUN_TIMEOUT_S = 15
DOWNLOAD_TIMEOUT_S = 600
#: Home DNS on this machine intermittently fails to resolve the Hugging
#: Face CDN host. Observed during development, hence real retries rather
#: than a single attempt.
DOWNLOAD_ATTEMPTS = 4
RETRY_DELAY_S = 2.0

# --- The engine -----------------------------------------------------------

#: Pinned. rhasspy/piper is archived at this release; development moved to
#: OHF-Voice/piper1-gpl, which is GPL-3.0 and ships as a Python package
#: rather than a standalone binary, so it does not help us here.
PIPER_RELEASE_TAG = "2023.11.14-2"
PIPER_ASSET = "piper_windows_amd64.zip"
PIPER_DOWNLOAD_URL = (
    f"https://github.com/rhasspy/piper/releases/download/{PIPER_RELEASE_TAG}/{PIPER_ASSET}"
)
#: Asset size reported by the GitHub releases API for the pinned tag. A
#: mismatch means the download was truncated or redirected somewhere else.
PIPER_ASSET_BYTES = 22_477_236
PIPER_RELEASE_URL = f"https://github.com/rhasspy/piper/releases/tag/{PIPER_RELEASE_TAG}"
PIPER_ENGINE_LICENCE = "MIT"

VOICES_REPO = "rhasspy/piper-voices"
VOICES_RESOLVE = f"https://huggingface.co/{VOICES_REPO}/resolve/main"
VOICES_RAW = f"https://huggingface.co/{VOICES_REPO}/raw/main"
VOICES_BROWSE_URL = f"https://huggingface.co/{VOICES_REPO}/tree/main"


@dataclass
class PiperStatus:
    exe_path: str | None = None
    version: str | None = None
    source: str = "not found"  # "settings" | "managed" | "PATH" | "not found"
    message: str = ""

    @property
    def available(self) -> bool:
        return bool(self.exe_path)


def tools_dir() -> Path:
    return get_settings().data_root / "tools"


def piper_dir() -> Path:
    return tools_dir() / "piper"


def managed_exe() -> Path:
    """Where this app installs piper, if it installs it at all."""
    return piper_dir() / "piper.exe"


def voices_dir() -> Path:
    return get_settings().data_root / "voices"


async def _run(executable: str, *args: str, timeout: int = RUN_TIMEOUT_S) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 126, "", str(exc)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", f"Timed out after {timeout}s."
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _resolve_exe(configured: str) -> tuple[Path | None, str]:
    text = (configured or "").strip().strip('"')
    if text:
        candidate = Path(text)
        if candidate.is_dir():
            candidate = candidate / "piper.exe"
        if candidate.is_file():
            return candidate, "settings"
        return None, "settings-invalid"

    managed = managed_exe()
    if managed.is_file():
        return managed, "managed"

    found = shutil.which("piper")
    if found:
        return Path(found), "PATH"
    return None, "not found"


async def detect(override: str = "") -> PiperStatus:
    """Locate piper.exe and confirm it actually executes."""
    configured = override or get_settings().piper_path
    path, source = _resolve_exe(configured)

    if path is None:
        if source == "settings-invalid":
            return PiperStatus(
                source=source,
                message=f"The configured piper path does not exist: {configured}",
            )
        return PiperStatus(
            source=source,
            message=(
                "Piper is not installed, so narration cannot be synthesised. "
                "Install it below, or set the path in Settings if you already have it."
            ),
        )

    status = PiperStatus(exe_path=str(path), source=source)
    # Piper has no --version flag in this release; --help is the cheapest
    # proof that the binary and its bundled runtime actually load.
    code, out, err = await _run(str(path), "--help")
    combined = f"{out}\n{err}"
    if code not in (0, 1) or "usage" not in combined.lower():
        status.exe_path = None
        status.message = (
            f"piper.exe was found at {path} but would not run: "
            f"{(err or out).strip()[:200] or f'exit code {code}'}"
        )
        return status

    status.version = f"rhasspy/piper {PIPER_RELEASE_TAG}" if source == "managed" else "present"
    status.message = f"Piper is available at {path}."
    return status


# --- Downloading ----------------------------------------------------------


class PiperInstallError(RuntimeError):
    """An install could not be completed. The message is shown to the user."""


def free_bytes(path: Path) -> int | None:
    """Free space on the drive holding ``path``, or None if unreadable."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _require_space(destination: Path, needed_bytes: int) -> None:
    free = free_bytes(destination)
    if free is None:
        return
    # Unzipping needs room for the archive and its contents at once.
    if free < needed_bytes:
        raise PiperInstallError(
            f"Not enough free space on {destination.anchor or destination}: "
            f"{free / 1_048_576:.0f} MB free, about {needed_bytes / 1_048_576:.0f} MB needed."
        )


async def _download(client: httpx.AsyncClient, url: str, destination: Path) -> int:
    """Stream ``url`` to ``destination``, retrying connection failures.

    Writes to a temporary file and moves it into place, so an interrupted
    download never leaves something that looks installed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error = ""

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        written = 0
        try:
            async with client.stream("GET", url, follow_redirects=True) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise PiperInstallError(
                        f"Download failed with HTTP {response.status_code}: {url}"
                    )
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes(262_144):
                        handle.write(chunk)
                        written += len(chunk)
        except (httpx.TransportError, httpx.HTTPError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            partial.unlink(missing_ok=True)
            if attempt < DOWNLOAD_ATTEMPTS:
                log.warning("Download attempt %s/%s failed for %s: %s",
                            attempt, DOWNLOAD_ATTEMPTS, url, last_error)
                await asyncio.sleep(RETRY_DELAY_S * attempt)
                continue
            raise PiperInstallError(
                f"Could not download {url} after {DOWNLOAD_ATTEMPTS} attempts. {last_error}"
            ) from exc

        partial.replace(destination)
        return written

    raise PiperInstallError(f"Could not download {url}. {last_error}")


async def install_engine(*, client: httpx.AsyncClient | None = None) -> PiperStatus:
    """Download and unpack the pinned Piper release into the data root."""
    target = piper_dir()
    _require_space(target, PIPER_ASSET_BYTES * 3)

    owned = client is None
    client = client or httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S)
    archive = tools_dir() / PIPER_ASSET
    try:
        size = await _download(client, PIPER_DOWNLOAD_URL, archive)
    finally:
        if owned:
            await client.aclose()

    if size != PIPER_ASSET_BYTES:
        archive.unlink(missing_ok=True)
        raise PiperInstallError(
            f"The download was {size} bytes but the pinned release asset is "
            f"{PIPER_ASSET_BYTES}. Nothing was installed."
        )

    staging = tools_dir() / "_piper_unpack"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        await asyncio.to_thread(_extract, archive, staging)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        archive.unlink(missing_ok=True)
        raise PiperInstallError(f"The archive could not be unpacked: {exc}") from exc

    found = next(staging.rglob("piper.exe"), None)
    if found is None:
        shutil.rmtree(staging, ignore_errors=True)
        raise PiperInstallError("The archive did not contain piper.exe.")

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    found.parent.replace(target)
    shutil.rmtree(staging, ignore_errors=True)
    archive.unlink(missing_ok=True)

    status = await detect()
    if not status.available:
        raise PiperInstallError(
            f"Piper was unpacked to {target} but would not run. {status.message}"
        )
    return status


def _extract(archive: Path, destination: Path) -> None:
    """Unzip, refusing any member that would escape the destination."""
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            resolved = (destination / member).resolve()
            if not str(resolved).startswith(str(destination.resolve())):
                raise PiperInstallError(f"The archive contains an unsafe path: {member}")
        bundle.extractall(destination)


# --- The voice catalogue --------------------------------------------------


@dataclass(frozen=True)
class CatalogueVoice:
    """A voice offered for one-click install.

    Deliberately carries no licence field. The licence is whatever the
    voice's own MODEL_CARD says at install time, and is read from there.
    """

    key: str
    display_name: str
    language: str
    quality: str
    repo_path: str
    #: Size of the .onnx reported by Hugging Face. Used for a space check
    #: and a progress estimate, not as a correctness assertion.
    approx_bytes: int
    summary: str = ""
    multi_speaker: bool = False
    #: Set when the card's own licence cannot be taken at face value because
    #: the voice was built on, or trained from, material whose terms this app
    #: has not checked. It forces the verdict to unknown and says why.
    unverified_reason: str = ""

    @property
    def model_url(self) -> str:
        return f"{VOICES_RESOLVE}/{self.repo_path}/{self.key}.onnx"

    @property
    def config_url(self) -> str:
        return f"{VOICES_RESOLVE}/{self.repo_path}/{self.key}.onnx.json"

    @property
    def model_card_url(self) -> str:
        return f"{VOICES_RAW}/{self.repo_path}/MODEL_CARD"

    @property
    def page_url(self) -> str:
        return f"{VOICES_BROWSE_URL}/{self.repo_path}"

    @property
    def install_dir(self) -> Path:
        return voices_dir() / self.key


#: A short, deliberately conservative shortlist. Every entry's repository
#: path and file size was confirmed against Hugging Face; the licence of
#: each is read from its MODEL_CARD during install rather than recorded
#: here. Any other Piper voice can still be used by pointing a profile at
#: a manually downloaded .onnx.
CATALOGUE: tuple[CatalogueVoice, ...] = (
    CatalogueVoice(
        key="en_US-joe-medium",
        display_name="Joe (US English, male)",
        language="en_US",
        quality="medium",
        repo_path="en/en_US/joe/medium",
        approx_bytes=63_201_294,
        summary=(
            "Steady male narration. Its card says CC0, but it was fine-tuned from "
            "the Lessac voice, whose dataset licence is research-only."
        ),
    ),
    CatalogueVoice(
        key="en_US-kristin-medium",
        display_name="Kristin (US English, female)",
        language="en_US",
        quality="medium",
        repo_path="en/en_US/kristin/medium",
        approx_bytes=63_531_379,
        summary="Female narration trained on LibriVox recordings.",
    ),
    CatalogueVoice(
        key="en_GB-cori-medium",
        display_name="Cori (UK English, female)",
        language="en_GB",
        quality="medium",
        repo_path="en/en_GB/cori/medium",
        approx_bytes=63_531_379,
        summary="British female narration trained on LibriVox recordings.",
    ),
    CatalogueVoice(
        key="en_US-libritts_r-medium",
        display_name="LibriTTS-R (US English, 904 speakers)",
        language="en_US",
        quality="medium",
        repo_path="en/en_US/libritts_r/medium",
        approx_bytes=78_580_914,
        summary="Multi-speaker. One download covers a whole cast, selected by speaker number. The card names CC BY 4.0, which requires attribution.",
        multi_speaker=True,
    ),
    CatalogueVoice(
        key="en_US-norman-medium",
        display_name="Norman (US English, male)",
        language="en_US",
        quality="medium",
        repo_path="en/en_US/norman/medium",
        approx_bytes=63_531_379,
        summary="Male narration. LibriVox recordings, trained from scratch.",
    ),
    CatalogueVoice(
        key="en_US-john-medium",
        display_name="John (US English, male)",
        language="en_US",
        quality="medium",
        repo_path="en/en_US/john/medium",
        approx_bytes=63_531_379,
        summary="Male narration. LibriVox recordings, fine-tuned from Kristin.",
    ),
    CatalogueVoice(
        key="ur_PK-fasih-medium",
        display_name="Fasih (Urdu, male)",
        language="ur_PK",
        quality="medium",
        repo_path="ur/ur_PK/fasih/medium",
        approx_bytes=64_832_023,
        summary=(
            "Urdu narration. The only Urdu voice in the catalogue, and male only. "
            "Its author rated it 3.3 out of 5 in a listening test."
        ),
        unverified_reason=(
            "The card says MIT, but this voice was fine-tuned from the Hindi Rohan "
            "voice, whose licence is an IIT Madras PDF, and it was trained on the "
            "UAT Urdu dataset. Neither has been checked, so the licence is treated "
            "as unknown until you check them."
        ),
    ),
    CatalogueVoice(
        key="hi_IN-rohan-medium",
        display_name="Rohan (Hindi, male)",
        language="hi_IN",
        quality="medium",
        repo_path="hi/hi_IN/rohan/medium",
        approx_bytes=62_950_044,
        summary=(
            "Hindi male narration. Its licence is a separate IIT Madras PDF, so the "
            "app reports it as unknown until you read that PDF."
        ),
    ),
    CatalogueVoice(
        key="hi_IN-pratham-medium",
        display_name="Pratham (Hindi, male)",
        language="hi_IN",
        quality="medium",
        repo_path="hi/hi_IN/pratham/medium",
        approx_bytes=63_516_050,
        summary="Hindi male narration. Trained on AI4Bharat data under CC BY-NC-SA, so non-commercial.",
    ),
    CatalogueVoice(
        key="hi_IN-priyamvada-medium",
        display_name="Priyamvada (Hindi, female)",
        language="hi_IN",
        quality="medium",
        repo_path="hi/hi_IN/priyamvada/medium",
        approx_bytes=63_516_050,
        summary="Hindi female narration. Trained on AI4Bharat data under CC BY-NC-SA, so non-commercial.",
    ),
)

CATALOGUE_BY_KEY: dict[str, CatalogueVoice] = {voice.key: voice for voice in CATALOGUE}


# --- Reading a model card -------------------------------------------------


@dataclass
class ModelCard:
    """What a voice's MODEL_CARD actually says, parsed but not interpreted."""

    text: str = ""
    licence: str = ""
    dataset_url: str = ""
    lineage: str = ""
    speakers: int | None = None
    sample_rate: int | None = None
    commercial_use: str = CommercialUse.UNKNOWN
    #: Why the classification came out the way it did. Shown to the user.
    licence_reason: str = ""
    #: True when the voice was fine-tuned from another, whose licence is a
    #: separate question this app cannot answer.
    has_lineage_caveat: bool = False
    warnings: list[str] = field(default_factory=list)


_LICENCE_RE = re.compile(r"^\s*[*-]?\s*License:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_URL_RE = re.compile(r"^\s*[*-]?\s*URL:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
_SPEAKERS_RE = re.compile(r"^\s*[*-]?\s*Speakers:\s*([\d,]+)", re.IGNORECASE | re.MULTILINE)
_RATE_RE = re.compile(r"^\s*[*-]?\s*Samplerate:\s*([\d,]+)", re.IGNORECASE | re.MULTILINE)
_LINEAGE_RE = re.compile(r"^(?:Fine-tuned|Finetuned|Trained) from .+$", re.IGNORECASE | re.MULTILINE)
#: Some cards state the licence under a "## License" heading, on the line below.
_LICENCE_SECTION_RE = re.compile(
    r"^#+[ \t]*Licen[cs]e[ \t]*\r?\n+[ \t]*([^\n#]+?)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
#: ...and some in the YAML header of the card, as "license: mit".
_LICENCE_YAML_RE = re.compile(r"^license:[ \t]*([^\n]+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)


def classify_licence(licence: str) -> tuple[str, str]:
    """Map a licence string to a commercial-use verdict and a reason.

    Only unambiguous strings are recognised. Anything else is reported as
    unknown, which blocks publishing until the user checks it themselves.
    Erring towards "unknown" is the point: a wrong "allowed" here would end
    up in a published video.
    """
    text = (licence or "").strip()
    if not text:
        return CommercialUse.UNKNOWN, "The model card states no licence."

    lowered = text.lower()

    # Non-commercial first: "by-nc-sa" also contains "by" and "sa".
    if re.search(r"\bnc\b|non-?commercial", lowered):
        return (
            CommercialUse.RESTRICTED,
            f"'{text}' is a non-commercial licence, so this voice cannot be used in a monetised video.",
        )
    if "cc0" in lowered or "public domain" in lowered:
        return CommercialUse.ALLOWED, f"'{text}' places no conditions on commercial use."
    if re.search(r"by[-\s]?sa", lowered):
        return (
            CommercialUse.CONDITIONAL,
            f"'{text}' permits commercial use with attribution and share-alike.",
        )
    if re.search(r"\bcc[-\s]?by\b", lowered) or "creativecommons.org/licenses/by/" in lowered:
        return CommercialUse.CONDITIONAL, f"'{text}' permits commercial use with attribution."
    if lowered.startswith("http") or "see url" in lowered:
        return (
            CommercialUse.UNKNOWN,
            f"The model card points at '{text}' instead of naming a licence. Read it before publishing.",
        )
    return (
        CommercialUse.UNKNOWN,
        f"'{text}' was not recognised. Check it yourself before publishing.",
    )


def parse_model_card(text: str) -> ModelCard:
    """Read a Piper MODEL_CARD. Records what it says; interprets only the licence."""
    card = ModelCard(text=text or "")
    if not text:
        card.licence_reason = "No model card was available for this voice."
        card.warnings.append(
            "No model card was downloaded, so this voice's licence is unknown."
        )
        return card

    licence_match = (
        _LICENCE_RE.search(text) or _LICENCE_SECTION_RE.search(text) or _LICENCE_YAML_RE.search(text)
    )
    card.licence = licence_match.group(1).strip() if licence_match else ""

    url_match = _URL_RE.search(text)
    card.dataset_url = url_match.group(1).strip() if url_match else ""

    speakers_match = _SPEAKERS_RE.search(text)
    if speakers_match:
        card.speakers = int(speakers_match.group(1).replace(",", ""))

    rate_match = _RATE_RE.search(text)
    if rate_match:
        card.sample_rate = int(rate_match.group(1).replace(",", ""))

    lineage_match = _LINEAGE_RE.search(text)
    if lineage_match:
        card.lineage = lineage_match.group(0).strip()
        # "Trained from scratch." has no upstream licence to worry about.
        if "from scratch" not in card.lineage.lower():
            card.has_lineage_caveat = True
            card.warnings.append(
                f"{card.lineage} The licence above covers the dataset; the voice it was "
                "fine-tuned from has its own, which this app has not checked."
            )

    card.commercial_use, card.licence_reason = classify_licence(card.licence)
    return card


def attribution_for(voice: CatalogueVoice, card: ModelCard) -> str:
    """The credit line to put in a description, when one is required."""
    parts = [f"Voice: {voice.key} from {VOICES_REPO}"]
    if card.licence:
        parts.append(f"licence {card.licence}")
    if card.dataset_url:
        parts.append(f"dataset {card.dataset_url}")
    return ", ".join(parts) + "."


@dataclass
class InstalledVoice:
    """The outcome of a voice install, before it becomes a database row."""

    voice: CatalogueVoice
    model_path: Path
    config_path: Path
    card: ModelCard
    bytes_downloaded: int = 0


async def install_voice(
    key: str, *, client: httpx.AsyncClient | None = None
) -> InstalledVoice:
    """Download one catalogue voice and read its licence from its model card."""
    voice = CATALOGUE_BY_KEY.get(key)
    if voice is None:
        raise PiperInstallError(f"'{key}' is not in the voice catalogue.")

    target = voice.install_dir
    _require_space(target, int(voice.approx_bytes * 1.3))

    owned = client is None
    client = client or httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S)
    model_path = target / f"{voice.key}.onnx"
    config_path = target / f"{voice.key}.onnx.json"
    card_path = target / "MODEL_CARD"
    try:
        downloaded = await _download(client, voice.model_url, model_path)
        await _download(client, voice.config_url, config_path)
        try:
            await _download(client, voice.model_card_url, card_path)
        except PiperInstallError as exc:
            # A missing card is not a failed install, but it does mean the
            # licence stays unknown, which the card parser will say.
            log.warning("No model card for %s: %s", voice.key, exc)
    finally:
        if owned:
            await client.aclose()

    if downloaded < 1_000_000:
        model_path.unlink(missing_ok=True)
        raise PiperInstallError(
            f"The voice file was only {downloaded} bytes, which is far too small to be a "
            "model. Nothing was installed."
        )

    card_text = ""
    if card_path.exists():
        card_text = card_path.read_text(encoding="utf-8", errors="replace")

    card = parse_model_card(card_text)
    if voice.unverified_reason:
        # The card may be right about this voice and still not settle its
        # licence. Keep what it says, but do not let it stand as the verdict.
        card.commercial_use = CommercialUse.UNKNOWN
        card.licence_reason = voice.unverified_reason

    return InstalledVoice(
        voice=voice,
        model_path=model_path,
        config_path=config_path,
        card=card,
        bytes_downloaded=downloaded,
    )


def install_instructions() -> dict[str, object]:
    """Shown when the automatic install is refused or fails."""
    return {
        "engine_url": PIPER_RELEASE_URL,
        "engine_asset": PIPER_ASSET,
        "voices_url": VOICES_BROWSE_URL,
        "target": str(piper_dir()),
        "voices_target": str(voices_dir()),
        "note": (
            f"Download {PIPER_ASSET} from the release page and unzip it so that "
            f"piper.exe sits directly in {piper_dir()}. Voices are a pair of files, "
            "an .onnx and its .onnx.json, which go in a folder of their own under "
            f"{voices_dir()}. Then press Re-check."
        ),
    }
