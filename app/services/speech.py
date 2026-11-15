"""Narration: turning an approved script into measured audio.

Two ideas carry this module.

The first is **one file per sentence**. Piper is invoked once per sentence,
so every subtitle cue has its own WAV. ``ffprobe`` then reports each
cue's real length, which is why timing downstream is measured rather than
estimated from a words-per-minute guess. It costs a process launch per
sentence and buys exact subtitles with no alignment model, which this
machine could not run anyway.

The second is **reuse by input hash**. A segment's hash covers its text
and every voice knob that changes the waveform. Re-running the stage after
editing one line re-synthesises that line and leaves the rest untouched,
so a five-minute script is never re-recorded wholesale. A new script
version keeps that same deal: a sentence that did not change is copied
from the recording already on disk instead of being spoken again.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    AudioKind,
    AudioStatus,
    CommercialUse,
    PipelineStage,
    SceneStatus,
    StageStatus,
)
from app.models import AudioSegment, Character, Project, Scene, VoiceProfile
from app.providers import speech as speech_registry
from app.providers.speech.base import SpeechProviderError, VoiceSettings
from app.services import (
    ffmpeg,
    piper as piper_tool,
    projects as project_service,
    scripts as script_service,
)
from app.services.paths import safe_filename

log = logging.getLogger(__name__)


class SpeechError(Exception):
    """Something the user needs to fix before narration can proceed."""


#: Longer than this and a subtitle cue is unreadable, so a sentence is
#: split at a clause boundary. Roughly two full subtitle lines.
MAX_CUE_CHARS = 180
#: Below this a fragment is glued onto its neighbour instead of becoming a
#: cue of its own, which avoids one-word flashes on screen.
MIN_CUE_CHARS = 12

#: Full stops and question marks for Urdu (U+06D4 and U+061F) and Hindi
#: (the danda, U+0964 and U+0965) are listed beside the Latin ones.
_SENTENCE_END = re.compile(r"(?<=[.!?\u2026\u06d4\u061f\u0964\u0965])[\"')\]]*\s+")
#: Abbreviations that end in a full stop without ending a sentence. Short
#: and deliberately incomplete; a wrong split costs a slightly odd cue
#: boundary, not a broken render.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt",
        "vs", "etc", "e.g", "i.e", "approx", "no", "fig",
    }
)
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:\u2014\u060c])\s+")


def split_sentences(text: str) -> list[str]:
    """Break a passage into subtitle-sized cues.

    Not a general-purpose sentence tokeniser, and does not need to be: the
    input is narration written by a language model to a schema, not
    arbitrary prose.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []

    pieces: list[str] = []
    for candidate in _SENTENCE_END.split(cleaned):
        candidate = candidate.strip()
        if not candidate:
            continue
        # Re-join a split that landed after an abbreviation.
        if pieces and _ends_with_abbreviation(pieces[-1]):
            pieces[-1] = f"{pieces[-1]} {candidate}"
        else:
            pieces.append(candidate)

    cues: list[str] = []
    for piece in pieces:
        cues.extend(_split_long(piece))

    # Absorb stragglers so no cue is a single flashed word.
    merged: list[str] = []
    for cue in cues:
        if merged and len(cue) < MIN_CUE_CHARS:
            merged[-1] = f"{merged[-1]} {cue}"
        else:
            merged.append(cue)
    return merged


def _ends_with_abbreviation(piece: str) -> bool:
    if not piece.endswith("."):
        return False
    last = piece.rsplit(" ", 1)[-1].rstrip(".").lower()
    return last in _ABBREVIATIONS


def _split_long(piece: str) -> list[str]:
    """Break an over-long sentence at clause boundaries, then by width."""
    if len(piece) <= MAX_CUE_CHARS:
        return [piece]

    out: list[str] = []
    current = ""
    for clause in _CLAUSE_SPLIT.split(piece):
        if not current:
            current = clause
        elif len(current) + 1 + len(clause) <= MAX_CUE_CHARS:
            current = f"{current} {clause}"
        else:
            out.append(current)
            current = clause
    if current:
        out.append(current)

    # A clause can still be too long on its own; fall back to word width.
    final: list[str] = []
    for chunk in out:
        if len(chunk) <= MAX_CUE_CHARS:
            final.append(chunk)
            continue
        words, line = chunk.split(" "), ""
        for word in words:
            if line and len(line) + 1 + len(word) > MAX_CUE_CHARS:
                final.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            final.append(line)
    return final


# --- Voice profiles -------------------------------------------------------


#: How much slower each beat is read, as a multiplier on the voice's own
#: pace. Small on purpose: a hook or a reveal lands by weight, not drawl.
PACE_BY_BEAT = {"hook": 1.04, "reveal": 1.08, "outro": 1.06}


def pace_for(project: Project, scene: Scene | None) -> float:
    """The delivery pace for a shot, from its beat, when the film is directed."""
    if scene is None or not (project.production or {}).get("enabled"):
        return 1.0
    return PACE_BY_BEAT.get(str((scene.direction or {}).get("beat") or ""), 1.0)


def voice_settings(profile: VoiceProfile, pace: float = 1.0) -> VoiceSettings:
    engine = profile.engine or "piper"
    return VoiceSettings(
        model_path=profile.model_path or "",
        config_path=profile.config_path or "",
        speaker_id=profile.speaker_id,
        length_scale=round(profile.length_scale * pace, 4),
        noise_scale=profile.noise_scale,
        noise_w=profile.noise_w,
        sentence_silence_s=profile.sentence_silence_s,
        engine=engine,
        voice_key=profile.name if engine in ("kokoro", "edge") else "",
    )


def segment_hash(text: str, voice: VoiceSettings) -> str:
    """Identifies audio that would come out identical if re-synthesised."""
    payload = f"{' '.join((text or '').split())}\x00{voice.fingerprint()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def list_voices(session: AsyncSession) -> list[VoiceProfile]:
    return list(
        (
            await session.execute(
                select(VoiceProfile).order_by(
                    VoiceProfile.is_default.desc(), VoiceProfile.name
                )
            )
        ).scalars().all()
    )


async def get_voice(session: AsyncSession, voice_id: int) -> VoiceProfile:
    row = await session.get(VoiceProfile, voice_id)
    if row is None:
        raise SpeechError(f"Voice {voice_id} does not exist.")
    return row


async def default_voice(session: AsyncSession) -> VoiceProfile | None:
    row = (
        await session.execute(
            select(VoiceProfile).where(
                VoiceProfile.is_default.is_(True), VoiceProfile.installed.is_(True)
            )
        )
    ).scalars().first()
    if row is not None:
        return row
    return (
        await session.execute(
            select(VoiceProfile)
            .where(VoiceProfile.installed.is_(True))
            .order_by(VoiceProfile.id)
        )
    ).scalars().first()


async def install_catalogue_voice(
    session: AsyncSession, key: str, *, client: Any = None
) -> VoiceProfile:
    """Download a catalogue voice and record what its model card says.

    The licence is whatever the card states. Nothing here decides a voice
    is safe to publish; the classification is conservative and an
    unrecognised licence stays unknown.
    """
    installed = await piper_tool.install_voice(key, client=client)
    card = installed.card
    voice = installed.voice

    row = (
        await session.execute(select(VoiceProfile).where(VoiceProfile.name == voice.key))
    ).scalars().first()
    if row is None:
        row = VoiceProfile(name=voice.key)
        session.add(row)

    row.engine = "piper"
    row.model_path = str(installed.model_path)
    row.config_path = str(installed.config_path)
    row.language = voice.language
    row.sample_rate = card.sample_rate
    row.licence = card.licence or None
    row.licence_url = card.dataset_url or None
    row.attribution = piper_tool.attribution_for(voice, card)
    row.commercial_use = card.commercial_use
    row.installed = True
    row.notes = "\n".join([card.licence_reason, *card.warnings]).strip() or None
    if voice.multi_speaker and row.speaker_id is None:
        row.speaker_id = 0

    existing = await session.scalar(select(func.count()).select_from(VoiceProfile))
    if existing in (0, 1) and not row.is_default:
        row.is_default = True

    await session.flush()
    return row


async def install_kokoro_voice(session: AsyncSession, voice_key: str) -> VoiceProfile:
    """Record one of Kokoro's built-in voices as a profile.

    The model files must already be in ``models/kokoro``. The voice is not
    made the default: Piper stays the default until the user chooses.
    """
    from app.providers.speech import kokoro

    folder = kokoro.models_dir()
    model, voices = folder / kokoro.MODEL_FILE, folder / kokoro.VOICES_FILE
    if not model.is_file() or not voices.is_file():
        raise SpeechError(f"Kokoro's model files are not in {folder}.")
    row = (
        await session.execute(select(VoiceProfile).where(VoiceProfile.name == voice_key))
    ).scalars().first()
    if row is None:
        row = VoiceProfile(name=voice_key)
        session.add(row)
    row.engine = "kokoro"
    row.model_path = str(model)
    row.config_path = str(voices)
    row.language = {"en-gb": "en_GB", "hi": "hi_IN"}.get(kokoro.language_for(voice_key), "en_US")
    row.sample_rate = kokoro.SAMPLE_RATE
    row.licence = "Apache-2.0"
    row.licence_url = "https://huggingface.co/hexgrad/Kokoro-82M"
    row.attribution = "Kokoro-82M by hexgrad, Apache-2.0"
    row.commercial_use = CommercialUse.ALLOWED
    row.installed = True
    row.notes = (
        "Weights Apache-2.0 per the model card. The speed is the inverse of "
        "length_scale; noise settings are not used by this engine."
    )
    await session.flush()
    return row


async def install_edge_voice(session: AsyncSession, voice_name: str) -> VoiceProfile:
    """Record one of Microsoft's online voices as a profile.

    Nothing is downloaded: the voice lives on Microsoft's service. Its terms
    were not checked, so the commercial verdict stays unknown.
    """
    from app.providers.speech import edge

    if voice_name not in edge.VOICES:
        raise SpeechError(f"'{voice_name}' is not one of the Edge voices offered here.")
    row = (
        await session.execute(select(VoiceProfile).where(VoiceProfile.name == voice_name))
    ).scalars().first()
    if row is None:
        row = VoiceProfile(name=voice_name)
        session.add(row)
    row.engine = "edge"
    row.model_path = ""
    row.config_path = ""
    row.language = edge.language_for(voice_name)
    row.sample_rate = edge.SAMPLE_RATE
    row.licence = "Microsoft service terms"
    row.licence_url = None
    row.attribution = f"Voice by Microsoft, generated through edge-tts ({voice_name})"
    row.commercial_use = CommercialUse.UNKNOWN
    row.installed = True
    row.notes = (
        "Generated by Microsoft's online service. Its terms have not been checked "
        "here, so commercial use is unknown until you check them."
    )
    await session.flush()
    return row


async def set_default_voice(session: AsyncSession, voice_id: int) -> VoiceProfile:
    voice = await get_voice(session, voice_id)
    for other in await list_voices(session):
        other.is_default = other.id == voice.id
    await session.flush()
    return voice


async def update_voice(session: AsyncSession, voice_id: int, **values: Any) -> VoiceProfile:
    """Adjust the synthesis knobs. Licence fields are read-only on purpose."""
    voice = await get_voice(session, voice_id)
    ranges: dict[str, tuple[float, float]] = {
        "length_scale": (0.3, 3.0),
        "noise_scale": (0.0, 2.0),
        "noise_w": (0.0, 2.0),
        "sentence_silence_s": (0.0, 3.0),
    }
    for name, (low, high) in ranges.items():
        if name not in values or values[name] in (None, ""):
            continue
        try:
            number = float(values[name])
        except (TypeError, ValueError) as exc:
            raise SpeechError(f"{name.replace('_', ' ')} must be a number.") from exc
        if not low <= number <= high:
            raise SpeechError(
                f"{name.replace('_', ' ')} must be between {low} and {high}."
            )
        setattr(voice, name, number)

    if values.get("speaker_id") not in (None, ""):
        try:
            voice.speaker_id = int(values["speaker_id"])
        except (TypeError, ValueError) as exc:
            raise SpeechError("Speaker must be a whole number.") from exc
    if values.get("name"):
        voice.name = str(values["name"]).strip()[:120]
    await session.flush()
    return voice


async def delete_voice(session: AsyncSession, voice_id: int) -> str:
    """Forget a voice. The downloaded files are left on disk deliberately."""
    voice = await get_voice(session, voice_id)
    name = voice.name
    in_use = await session.scalar(
        select(func.count())
        .select_from(Project)
        .where(Project.narrator_voice_id == voice.id)
    )
    if in_use:
        raise SpeechError(
            f"'{name}' is still the narrator for {in_use} project(s). Change those first."
        )
    await session.delete(voice)
    await session.flush()
    return name


# --- Planning the segments ------------------------------------------------


@dataclass
class SpeechPlan:
    """What the next synthesis run would do, before it does any of it."""

    total: int = 0
    reusable: int = 0
    to_synthesise: int = 0
    removed: int = 0
    scenes_without_voice: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.to_synthesise == 0 and self.removed == 0


def _audio_dir(project: Project) -> Path:
    from app.config import get_settings

    folder = safe_filename(f"{project.id:04d}-{project.name}", fallback=f"{project.id:04d}")
    return get_settings().data_root / "speech" / folder


async def _voice_for(
    session: AsyncSession,
    project: Project,
    character_name: str | None,
    characters: dict[str, Character],
    fallback: VoiceProfile | None,
) -> VoiceProfile | None:
    if character_name:
        character = characters.get(character_name.strip().lower())
        if character is not None and character.voice_profile_id:
            return await session.get(VoiceProfile, character.voice_profile_id)
    if project.narrator_voice_id:
        narrator = await session.get(VoiceProfile, project.narrator_voice_id)
        if narrator is not None:
            return narrator
    return fallback


async def plan_segments(session: AsyncSession, project_id: int) -> SpeechPlan:
    """Create, update and prune ``audio_segments`` for the approved script.

    Idempotent. A segment whose text and voice are unchanged keeps its
    file and its measured duration; everything else is queued.
    """
    project = await project_service.get_project(session, project_id)
    script = await script_service.approved_script(session, project_id)
    if script is None:
        raise SpeechError(
            "No approved script. Approve a script version before synthesising narration."
        )

    scenes = list(
        (
            await session.execute(
                select(Scene)
                .where(Scene.script_id == script.id)
                .order_by(Scene.order_index)
            )
        ).scalars().all()
    )
    characters = {
        row.name.strip().lower(): row
        for row in (
            await session.execute(select(Character).where(Character.project_id == project_id))
        ).scalars().all()
    }
    fallback = await default_voice(session)

    existing = list(
        (
            await session.execute(
                select(AudioSegment).where(AudioSegment.project_id == project_id)
            )
        ).scalars().all()
    )
    by_scene: dict[int, list[AudioSegment]] = {}
    for row in existing:
        by_scene.setdefault(row.scene_id, []).append(row)
    for rows in by_scene.values():
        rows.sort(key=lambda r: r.order_index)

    plan = SpeechPlan()
    audio_root = _audio_dir(project)
    keep_ids: set[int] = set()
    kept_paths: set[str] = set()
    # Recordings from an older script version, keyed by the hash of the
    # line and the voice. A new scene row can adopt one instead of
    # speaking the same sentence again.
    bank: dict[str, AudioSegment] = {}
    for row in existing:
        if (
            row.status == AudioStatus.READY
            and row.input_hash
            and row.file_path
            and row.duration_s is not None
            and Path(row.file_path).exists()
        ):
            bank.setdefault(row.input_hash, row)

    for scene in scenes:
        wanted: list[tuple[str, str, Character | None]] = []
        for cue in split_sentences(scene.narration or ""):
            wanted.append((AudioKind.NARRATION, cue, None))
        for line in scene.dialogue or []:
            if not isinstance(line, dict):
                continue
            speaker = str(line.get("character") or "").strip()
            character = characters.get(speaker.lower())
            for cue in split_sentences(str(line.get("line") or "")):
                wanted.append((AudioKind.DIALOGUE, cue, character))

        if wanted:
            probe_voice = await _voice_for(
                session, project, wanted[0][2].name if wanted[0][2] else None,
                characters, fallback,
            )
            if probe_voice is None:
                plan.scenes_without_voice.append(scene.scene_key or str(scene.order_index))

        current = by_scene.get(scene.id, [])
        for index, (kind, text, character) in enumerate(wanted):
            voice = await _voice_for(
                session, project, character.name if character else None, characters, fallback
            )
            row = current[index] if index < len(current) else None
            if row is None:
                row = AudioSegment(
                    scene_id=scene.id,
                    project_id=project_id,
                    order_index=index,
                    text=text,
                )
                session.add(row)
                await session.flush()
            keep_ids.add(row.id)
            plan.total += 1

            row.order_index = index
            row.kind = kind
            row.text = text
            row.character_id = character.id if character else None
            row.voice_profile_id = voice.id if voice else None

            if voice is None:
                row.status = AudioStatus.PENDING
                row.file_path = None
                row.duration_s = None
                row.input_hash = None
                plan.to_synthesise += 1
                continue

            wanted_hash = segment_hash(text, voice_settings(voice, pace_for(project, scene)))
            reusable = (
                row.status == AudioStatus.READY
                and row.input_hash == wanted_hash
                and row.file_path
                and Path(row.file_path).exists()
                and row.duration_s is not None
            )
            if reusable:
                plan.reusable += 1
                if row.file_path:
                    kept_paths.add(row.file_path)
                continue

            donor = bank.get(wanted_hash)
            if donor is not None and donor.id != row.id and _adopt_recording(
                row, donor, audio_root, scene.order_index, index, kind
            ):
                plan.reusable += 1
                if row.file_path:
                    kept_paths.add(row.file_path)
                continue

            row.input_hash = wanted_hash
            row.status = AudioStatus.PENDING
            row.duration_s = None
            row.error = None
            row.file_path = str(
                audio_root
                / f"s{scene.order_index:03d}-{index:02d}-{kind}-{row.id}.wav"
            )
            plan.to_synthesise += 1

    stale = [row for row in existing if row.id not in keep_ids]
    if stale:
        plan.removed = len(stale)
        for row in stale:
            if row.file_path and row.file_path not in kept_paths:
                Path(row.file_path).unlink(missing_ok=True)
        await session.execute(
            delete(AudioSegment).where(AudioSegment.id.in_([row.id for row in stale]))
        )

    await session.flush()
    return plan


# --- Synthesis ------------------------------------------------------------


@dataclass
class SpeechRun:
    """The measured outcome of one synthesis pass. Nothing here is estimated."""

    attempted: int = 0
    synthesised: int = 0
    reused: int = 0
    failed: int = 0
    measured_s: float = 0.0
    unmeasured: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.unmeasured == 0


async def _measure(path: Path, ffprobe_path: str | None) -> tuple[float | None, str]:
    info = await ffmpeg.probe(path, ffprobe_path)
    if info.error or info.duration_s is None:
        return None, info.error or "ffprobe returned no duration."
    return info.duration_s, ""


def _adopt_recording(
    row: AudioSegment,
    donor: AudioSegment,
    audio_root: Path,
    scene_order: int,
    index: int,
    kind: str,
) -> bool:
    """Point a new segment at a copy of a recording that already matches it.

    The path column is unique, so two segments cannot share one file. The
    copy is the new segment's own file. The donor's file is left for the
    caller to remove once nothing still points at it.
    """
    source = Path(donor.file_path or "")
    if donor.duration_s is None or not source.is_file():
        return False
    audio_root.mkdir(parents=True, exist_ok=True)
    destination = audio_root / f"s{scene_order:03d}-{index:02d}-{kind}-{row.id}.wav"
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    row.file_path = str(destination)
    row.duration_s = donor.duration_s
    row.sample_rate = donor.sample_rate
    row.input_hash = donor.input_hash
    row.status = AudioStatus.READY
    row.error = None
    return True


async def synthesise_project(
    session: AsyncSession, project_id: int, *, limit: int | None = None
) -> SpeechRun:
    """Synthesise every pending segment and measure each result.

    A segment whose WAV cannot be measured is reported as unmeasured
    rather than having a length guessed for it, because every subtitle cue
    downstream depends on these numbers being real.
    """
    await project_service.mark_stage(
        session, project_id, PipelineStage.SPEECH, StageStatus.RUNNING
    )

    provider = speech_registry.get()
    run = SpeechRun()
    if provider is None:
        run.errors.append("The Piper speech provider failed to load.")
        await project_service.mark_stage(
            session, project_id, PipelineStage.SPEECH, StageStatus.FAILED,
            message=run.errors[-1],
        )
        return run

    available, why = await provider.is_available()
    if not available:
        await project_service.mark_stage(
            session, project_id, PipelineStage.SPEECH, StageStatus.BLOCKED,
            message=why, blocked_reason=why,
        )
        run.errors.append(why)
        return run

    ffprobe_status = await ffmpeg.detect()
    if not ffprobe_status.probe_available:
        why = (
            "FFprobe is not installed, so segment lengths cannot be measured. "
            "Subtitle timing depends on real measurements, so narration is on hold."
        )
        await project_service.mark_stage(
            session, project_id, PipelineStage.SPEECH, StageStatus.BLOCKED,
            message=why, blocked_reason=why,
        )
        run.errors.append(why)
        return run

    pending = list(
        (
            await session.execute(
                select(AudioSegment)
                .options(selectinload(AudioSegment.voice_profile))
                .where(
                    AudioSegment.project_id == project_id,
                    AudioSegment.status != AudioStatus.READY,
                )
                .order_by(AudioSegment.scene_id, AudioSegment.order_index)
            )
        ).scalars().all()
    )
    if limit is not None:
        pending = pending[:limit]

    for segment in pending:
        run.attempted += 1
        await _synthesise_one(session, segment, provider, ffprobe_status.ffprobe_path, run)
        # Committed per segment: an interrupted run keeps everything it
        # already recorded instead of starting over.
        await session.commit()

    await _refresh_scene_audio(session, project_id)
    # Each run teaches the app how fast this voice really is, so the next
    # script is written to a measured rate instead of an assumed one.
    for voice in await voices_in_use(session, project_id):
        await refresh_measured_rate(session, voice.id)
    totals = await measured_totals(session, project_id)
    run.measured_s = totals["measured_s"]
    run.unmeasured = totals["unmeasured"]
    run.reused = totals["ready"] - run.synthesised

    status, message = _stage_outcome(run, totals)
    await project_service.mark_stage(
        session, project_id, PipelineStage.SPEECH, status,
        message=message,
        payload={
            "segments": totals["total"],
            "ready": totals["ready"],
            "failed": totals["failed"],
            "measured_s": round(totals["measured_s"], 3),
        },
    )
    await project_service.refresh_status(session, project_id)
    await session.commit()
    return run


def _stage_outcome(run: SpeechRun, totals: dict[str, Any]) -> tuple[str, str]:
    if totals["total"] == 0:
        return StageStatus.PENDING, "There is nothing to narrate yet."
    if totals["ready"] == totals["total"]:
        return (
            StageStatus.COMPLETED,
            f"{totals['ready']} segment(s), {totals['measured_s']:.1f}s of measured narration.",
        )
    if totals["ready"] == 0:
        return (
            StageStatus.FAILED,
            f"No segment could be synthesised. {run.errors[0] if run.errors else ''}".strip(),
        )
    return (
        StageStatus.PARTIAL,
        f"{totals['ready']} of {totals['total']} segment(s) done, "
        f"{totals['failed']} failed. {totals['measured_s']:.1f}s measured so far.",
    )


async def _synthesise_one(
    session: AsyncSession,
    segment: AudioSegment,
    provider: Any,
    ffprobe_path: str | None,
    run: SpeechRun,
) -> None:
    voice = segment.voice_profile
    # Piper voices are a model file on disk. Kokoro and Edge voices name a
    # voice inside an engine, so they have no model file of their own.
    is_piper = voice is None or (voice.engine or "piper") == "piper"
    if voice is None or (is_piper and not voice.model_path):
        segment.status = AudioStatus.FAILED
        segment.error = "No voice is assigned to this line."
        run.failed += 1
        run.errors.append(segment.error)
        return

    project = await project_service.get_project(session, segment.project_id)
    scene = await session.get(Scene, segment.scene_id) if segment.scene_id else None
    settings = voice_settings(voice, pace_for(project, scene))
    if settings.engine != getattr(provider, "key", "piper"):
        engine = speech_registry.get(settings.engine)
        if engine is None:
            segment.status = AudioStatus.FAILED
            segment.error = f"The {settings.engine} speech engine is not available."
            run.failed += 1
            run.errors.append(segment.error)
            return
        available, why = await engine.is_available()
        if not available:
            segment.status = AudioStatus.FAILED
            segment.error = why
            run.failed += 1
            run.errors.append(why)
            return
        provider = engine
    if not segment.file_path:
        segment.file_path = str(_audio_dir(project) / f"segment-{segment.id}.wav")
    destination = Path(segment.file_path)

    segment.status = AudioStatus.SYNTHESISING
    await session.flush()

    try:
        result = await provider.synthesise(segment.text, settings, destination)
    except SpeechProviderError as exc:
        segment.status = AudioStatus.FAILED
        segment.error = exc.message
        run.failed += 1
        run.errors.append(f"{destination.name}: {exc.message}")
        return

    duration, problem = await _measure(result.path, ffprobe_path)
    if duration is None:
        segment.status = AudioStatus.FAILED
        segment.error = f"The file was written but could not be measured. {problem}"
        run.failed += 1
        run.errors.append(f"{destination.name}: {problem}")
        return

    segment.duration_s = duration
    segment.sample_rate = voice.sample_rate
    segment.input_hash = segment_hash(segment.text, settings)
    segment.status = AudioStatus.READY
    segment.error = None
    run.synthesised += 1


async def regenerate_segment(session: AsyncSession, segment_id: int) -> AudioSegment:
    """Re-record exactly one line, leaving every other segment alone."""
    segment = await session.get(AudioSegment, segment_id)
    if segment is None:
        raise SpeechError(f"Segment {segment_id} does not exist.")

    provider = speech_registry.get()
    if provider is None:
        raise SpeechError("The Piper speech provider failed to load.")
    available, why = await provider.is_available()
    if not available:
        raise SpeechError(why)

    ffprobe_status = await ffmpeg.detect()
    if not ffprobe_status.probe_available:
        raise SpeechError(
            "FFprobe is not installed, so the new take could not be measured."
        )

    await session.refresh(segment, ["voice_profile"])
    run = SpeechRun()
    # Force a fresh take even when the text and voice are unchanged: this
    # is the button for "that take was wrong", not a cache check.
    if segment.file_path:
        Path(segment.file_path).unlink(missing_ok=True)
    await _synthesise_one(session, segment, provider, ffprobe_status.ffprobe_path, run)

    await _refresh_scene_audio(session, segment.project_id)
    await session.flush()
    if run.failed:
        raise SpeechError(run.errors[0] if run.errors else "Synthesis failed.")
    return segment


async def _refresh_scene_audio(session: AsyncSession, project_id: int) -> None:
    """Roll measured segment lengths up onto their scenes.

    A scene with any unmeasured segment keeps a null total rather than a
    partial one, so a half-recorded scene is never mistaken for a short one.
    """
    scenes = list(
        (
            await session.execute(select(Scene).where(Scene.project_id == project_id))
        ).scalars().all()
    )
    rows = list(
        (
            await session.execute(
                select(AudioSegment).where(AudioSegment.project_id == project_id)
            )
        ).scalars().all()
    )
    grouped: dict[int, list[AudioSegment]] = {}
    for row in rows:
        grouped.setdefault(row.scene_id, []).append(row)

    for scene in scenes:
        segments = grouped.get(scene.id, [])
        if not segments:
            scene.measured_audio_s = None
            continue
        if any(s.status != AudioStatus.READY or s.duration_s is None for s in segments):
            scene.measured_audio_s = None
            continue
        scene.measured_audio_s = round(sum(s.duration_s or 0.0 for s in segments), 3)
        if scene.status == SceneStatus.PLANNED:
            scene.status = SceneStatus.VOICED
    await session.flush()


#: Below this many recorded lines a rate is too noisy to write scripts to.
MIN_RATE_SAMPLES = 8


async def refresh_measured_rate(session: AsyncSession, voice_id: int) -> VoiceProfile:
    """Work out how fast a voice really speaks, from what it has recorded.

    This exists because a sixty-second brief produced a ninety-four second
    video: the script had been written to an assumed 150 words a minute
    while the voice actually delivers about 180. Guessing a better number
    would only move the error, so the figure is taken from finished audio
    and stays null until there is enough of it to mean anything.
    """
    voice = await get_voice(session, voice_id)
    rows = list(
        (
            await session.execute(
                select(AudioSegment).where(
                    AudioSegment.voice_profile_id == voice_id,
                    AudioSegment.status == AudioStatus.READY,
                    AudioSegment.duration_s.is_not(None),
                )
            )
        ).scalars().all()
    )
    total_s = sum(row.duration_s or 0.0 for row in rows)
    if len(rows) < MIN_RATE_SAMPLES or total_s <= 0:
        voice.measured_segments = len(rows)
        voice.measured_wpm = None
        voice.measured_chars_per_s = None
        await session.flush()
        return voice

    words = sum(len(row.text.split()) for row in rows)
    chars = sum(len(row.text) for row in rows)
    voice.measured_segments = len(rows)
    voice.measured_wpm = round(words / total_s * 60.0, 1)
    voice.measured_chars_per_s = round(chars / total_s, 2)
    await session.flush()
    return voice


async def writing_rate_wpm(session: AsyncSession, project: Project) -> tuple[int, str]:
    """The words-per-minute a script should be written to, and why.

    Prefers the rate the project's own voice was measured at. Falls back
    to the project setting, saying plainly that it is an assumption.
    """
    voice_id = project.narrator_voice_id
    voice = await session.get(VoiceProfile, voice_id) if voice_id else None
    if voice is None:
        voice = await default_voice(session)
    if voice is not None and voice.measured_wpm:
        return int(round(voice.measured_wpm)), (
            f"Measured from {voice.measured_segments} line(s) already recorded with "
            f"{voice.name}."
        )
    return project.speaking_rate_wpm, (
        "An assumption, because this voice has not recorded enough lines to be "
        "measured yet. Expect the finished video to run longer or shorter than planned."
    )


async def measured_totals(session: AsyncSession, project_id: int) -> dict[str, Any]:
    """Counts and seconds straight from the rows. Nothing extrapolated."""
    rows = list(
        (
            await session.execute(
                select(AudioSegment).where(AudioSegment.project_id == project_id)
            )
        ).scalars().all()
    )
    ready = [r for r in rows if r.status == AudioStatus.READY and r.duration_s is not None]
    return {
        "total": len(rows),
        "ready": len(ready),
        "failed": sum(1 for r in rows if r.status == AudioStatus.FAILED),
        "pending": sum(1 for r in rows if r.status == AudioStatus.PENDING),
        "unmeasured": len(rows) - len(ready),
        "measured_s": round(sum(r.duration_s or 0.0 for r in ready), 3),
    }


# --- Licence reporting ----------------------------------------------------


def licence_warnings(voices: list[VoiceProfile]) -> list[str]:
    """Plain statements about what the voices in use permit.

    Phrased as what the licence says, not as legal advice, and silent
    about voices whose licence was recognised as unconditional.
    """
    out: list[str] = []
    for voice in voices:
        if voice.commercial_use == CommercialUse.RESTRICTED:
            out.append(
                f"{voice.name}: its model card gives a non-commercial licence "
                f"({voice.licence or 'unstated'}), so it should not be used in a monetised video."
            )
        elif voice.commercial_use == CommercialUse.UNKNOWN:
            out.append(
                f"{voice.name}: the licence ({voice.licence or 'unstated'}) was not recognised, "
                "so it is treated as unknown. Check it before publishing."
            )
        elif voice.commercial_use == CommercialUse.CONDITIONAL:
            out.append(
                f"{voice.name}: commercial use is permitted with attribution. "
                f"Credit line: {voice.attribution or voice.licence or 'see model card'}"
            )
    return out


async def voices_in_use(session: AsyncSession, project_id: int) -> list[VoiceProfile]:
    ids = set(
        (
            await session.execute(
                select(AudioSegment.voice_profile_id)
                .where(
                    AudioSegment.project_id == project_id,
                    AudioSegment.voice_profile_id.is_not(None),
                )
                .distinct()
            )
        ).scalars().all()
    )
    project = await project_service.get_project(session, project_id)
    if project.narrator_voice_id:
        ids.add(project.narrator_voice_id)
    if not ids:
        return []
    return list(
        (
            await session.execute(
                select(VoiceProfile).where(VoiceProfile.id.in_(ids)).order_by(VoiceProfile.name)
            )
        ).scalars().all()
    )


# --- Screen context -------------------------------------------------------


async def voices_context(session: AsyncSession) -> dict[str, Any]:
    from app.providers.speech import edge, kokoro

    status = await piper_tool.detect()
    installed = await list_voices(session)
    installed_names = {row.name for row in installed}
    kokoro_folder = kokoro.models_dir()
    kokoro_ready = all(
        (kokoro_folder / name).is_file() for name in (kokoro.MODEL_FILE, kokoro.VOICES_FILE)
    )
    return {
        "kokoro": {
            "ready": kokoro_ready,
            "folder": str(kokoro_folder),
            "voices": [
                {
                    "key": key,
                    "language": {"en-gb": "English (UK)", "hi": "Hindi"}.get(
                        kokoro.language_for(key), "English (US)"
                    ),
                    "installed": key in installed_names,
                }
                for key in (*kokoro.NARRATORS, *kokoro.HINDI_VOICES)
            ],
        },
        "edge": {
            "voices": [
                {
                    "key": name,
                    "language": edge.language_for(name).replace("_", "-"),
                    "installed": name in installed_names,
                }
                for name in edge.VOICES
            ],
        },
        "piper": status,
        "voices": installed,
        "catalogue": [
            {
                "voice": entry,
                "installed": entry.key in installed_names,
                "approx_mb": round(entry.approx_bytes / 1_048_576),
            }
            for entry in piper_tool.CATALOGUE
        ],
        "engine": speech_registry.describe(),
        "instructions": piper_tool.install_instructions(),
        "licence_warnings": licence_warnings(installed),
        "voices_dir": str(piper_tool.voices_dir()),
        "min_rate_samples": MIN_RATE_SAMPLES,
    }


async def narration_context(session: AsyncSession, project_id: int) -> dict[str, Any]:
    project = await project_service.get_project(session, project_id)
    script = await script_service.approved_script(session, project_id)
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SPEECH
    )
    totals = await measured_totals(session, project_id)

    scenes: list[Scene] = []
    segments: dict[int, list[AudioSegment]] = {}
    if script is not None:
        scenes = list(
            (
                await session.execute(
                    select(Scene)
                    .where(Scene.script_id == script.id)
                    .order_by(Scene.order_index)
                )
            ).scalars().all()
        )
        rows = list(
            (
                await session.execute(
                    select(AudioSegment)
                    .options(selectinload(AudioSegment.voice_profile))
                    .where(AudioSegment.project_id == project_id)
                    .order_by(AudioSegment.scene_id, AudioSegment.order_index)
                )
            ).scalars().all()
        )
        for row in rows:
            segments.setdefault(row.scene_id, []).append(row)

    used = await voices_in_use(session, project_id)
    narrator = (
        await session.get(VoiceProfile, project.narrator_voice_id)
        if project.narrator_voice_id
        else None
    )
    return {
        "project": project,
        "script": script,
        "scenes": scenes,
        "segments": segments,
        "checkpoint": checkpoint,
        "totals": totals,
        "narrator": narrator,
        "voices": await list_voices(session),
        "piper": await piper_tool.detect(),
        "licence_warnings": licence_warnings(used),
        "target_duration_s": project.target_duration_s,
    }
