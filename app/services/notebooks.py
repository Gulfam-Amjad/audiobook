"""Notebooks sent to borrowed GPU time.

These are the only place in the project where code is written to run on
somebody else's machine, so a few rules are enforced here rather than
trusted to the notebook author.

**The weights are pinned and cached.** A free session that spends ten
minutes re-downloading a checkpoint has spent ten minutes of the weekly
allowance on nothing. The notebook mounts a Kaggle Dataset holding the
weights and only falls back to downloading when that dataset is absent,
saying clearly in its log which of the two happened.

**Every run writes a manifest.** ``/kaggle/working/manifest.json`` records
what was produced, how long each clip is, and whether the model actually
ran. The app reads that rather than inferring success from the presence
of a file, because a notebook that crashed halfway can still leave files
behind.

**A failed shot fails loudly.** The notebook never substitutes a still
for a clip it could not animate. Falling back to a camera move is the
app's decision, made locally, where it can be labelled honestly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Where a Kaggle notebook may write.
WORKING_DIR = "/kaggle/working"
MANIFEST_NAME = "manifest.json"

#: Dataset holding pinned model weights, mounted read-only. Created by
#: the user with `kaggle datasets create`; the notebook copes without it.
WEIGHTS_DATASET_HINT = "avm-model-weights"


@dataclass(frozen=True)
class NotebookSpec:
    """What a notebook needs to know before it is rendered."""

    purpose: str
    #: Base64 JSON blob of inputs, produced by the compute adapter.
    inputs_json: str
    #: Per-shot instructions: duration, prompt, which input to use.
    shots: list[dict[str, Any]]
    #: Where the mounted weights dataset is expected, if one is attached.
    weights_dir: str = ""
    fps: int = 24
    width: int = 512
    height: int = 512


def _cell(source: str) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


def _notebook(cells: list[dict[str, Any]]) -> str:
    """A minimal nbformat 4 document. Kaggle takes the JSON as a string."""
    return json.dumps(
        {
            "cells": cells,
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3",
                },
                "language_info": {"name": "python", "version": "3.11"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        },
        indent=1,
    )


#: These templates are Python source that itself contains braces and
#: f-strings, so ``str.format`` is not usable here. Substitution is done
#: on explicit ``@@NAME@@`` markers instead, which cannot collide with
#: anything the notebook code legitimately contains.
def _fill(template: str, **values: Any) -> str:
    text = template
    for name, value in values.items():
        text = text.replace(f"@@{name.upper()}@@", str(value))
    if "@@" in text:
        leftover = text[text.index("@@") : text.index("@@") + 30]
        raise ValueError(f"A notebook placeholder was never filled in: {leftover!r}")
    return text


_PREAMBLE = '''"""Written by AI Video Manager. Do not edit in place; it is regenerated."""
import base64, json, os, subprocess, sys, time, traceback
from pathlib import Path

WORKING = Path("@@WORKING@@")
WORKING.mkdir(parents=True, exist_ok=True)
INPUT_DIR = WORKING / "inputs"
INPUT_DIR.mkdir(exist_ok=True)

started = time.time()
manifest = {
    "purpose": "@@PURPOSE@@",
    "started_at": time.time(),
    "shots": [],
    "problems": [],
    # Set to True only after a model has actually produced motion. The app
    # refuses to count a clip as generated motion without it.
    "model_ran": False,
    "weights_source": "unknown",
}


def write_manifest():
    manifest["finished_at"] = time.time()
    manifest["elapsed_s"] = round(time.time() - started, 2)
    (WORKING / "@@MANIFEST_NAME@@").write_text(json.dumps(manifest, indent=2))


def note_problem(where, exc):
    detail = f"{where}: {type(exc).__name__}: {exc}"
    print("PROBLEM", detail, flush=True)
    manifest["problems"].append(detail)


# Inputs travel inside this notebook, so a run needs no extra upload step.
for item in json.loads(base64.b64decode("@@INPUTS@@").decode("utf-8")):
    target = INPUT_DIR / item["name"]
    target.write_bytes(base64.b64decode(item["b64"]))
    print("input", target.name, target.stat().st_size, "bytes", flush=True)

SHOTS = json.loads(r"""@@SHOTS@@""")
print(f"{len(SHOTS)} shot(s) to produce", flush=True)
'''


_WEIGHTS = '''
# Prefer weights mounted from a Kaggle Dataset. Re-downloading a
# checkpoint every run would spend the weekly GPU allowance on transfers.
_weights_dir = "@@WEIGHTS_DIR@@"
WEIGHTS = Path(_weights_dir) if _weights_dir else None
if WEIGHTS and WEIGHTS.exists():
    manifest["weights_source"] = f"dataset:{WEIGHTS}"
    print("Using cached weights from", WEIGHTS, flush=True)
else:
    manifest["weights_source"] = "download"
    print(
        "No weights dataset is mounted, so this run downloads them. "
        "Attach a dataset to avoid spending the allowance on transfers.",
        flush=True,
    )
'''


_GPU_CHECK = '''
# Report the GPU honestly: a notebook that silently fell back to the CPU
# would take hours and look like a hang.
try:
    import torch

    manifest["cuda"] = bool(torch.cuda.is_available())
    manifest["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    print("CUDA:", manifest["cuda"], manifest.get("gpu"), flush=True)
    if not manifest["cuda"]:
        manifest["problems"].append(
            "No GPU was attached to this session, so the model would run on the CPU."
        )
except Exception as exc:
    note_problem("gpu-check", exc)
'''


_I2V_BODY = '''
# --- Image to video -------------------------------------------------------
# The model is installed at run time because the session starts clean.
try:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "diffusers>=0.31", "accelerate"],
        check=True,
    )
    import torch
    from diffusers import StableVideoDiffusionPipeline
    from diffusers.utils import export_to_video
    from PIL import Image

    model_id = "stabilityai/stable-video-diffusion-img2vid-xt"
    source = str(WEIGHTS / "svd-xt") if manifest["weights_source"].startswith("dataset") else model_id
    pipe = StableVideoDiffusionPipeline.from_pretrained(
        source, torch_dtype=torch.float16, variant="fp16"
    )
    pipe.to("cuda")
    pipe.enable_model_cpu_offload()
except Exception as exc:
    note_problem("setup", exc)
    write_manifest()
    raise

for shot in SHOTS:
    out = WORKING / (shot["scene_key"] + ".mp4")
    try:
        image = Image.open(INPUT_DIR / shot["image"]).convert("RGB")
        image = image.resize((@@WIDTH@@, @@HEIGHT@@))
        frames = pipe(
            image,
            decode_chunk_size=4,
            num_frames=min(25, max(8, int(round(shot["duration_s"] * @@FPS@@)))),
            motion_bucket_id=shot.get("motion_bucket", 127),
            noise_aug_strength=0.02,
            generator=torch.manual_seed(shot.get("seed", 0)),
        ).frames[0]
        export_to_video(frames, str(out), fps=@@FPS@@)
        manifest["model_ran"] = True
        manifest["shots"].append(
            {
                "scene_key": shot["scene_key"],
                "file": out.name,
                "frames": len(frames),
                "fps": @@FPS@@,
                # The app measures the real duration with ffprobe once the
                # file is back. This is only what the notebook intended.
                "requested_duration_s": shot["duration_s"],
                "generated_motion": True,
            }
        )
        print("rendered", out.name, len(frames), "frames", flush=True)
    except Exception as exc:
        # No substitute is written. A missing clip is the app's problem to
        # solve locally, where the fallback can be labelled honestly.
        note_problem(shot["scene_key"], exc)
        manifest["shots"].append(
            {"scene_key": shot["scene_key"], "file": None, "generated_motion": False,
             "error": f"{type(exc).__name__}: {exc}"}
        )

write_manifest()
print(json.dumps(manifest, indent=2), flush=True)
'''


_LIPSYNC_BODY = '''
# --- Lip sync -------------------------------------------------------------
# This cannot run on the machine the app lives on: MuseTalk's own minimum
# tested configuration is 4 GB of VRAM and that machine has 2 GB.
try:
    import torch

    repo = Path("/kaggle/working/MuseTalk")
    if not repo.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/TMElyralab/MuseTalk", str(repo)],
            check=True,
        )
    sys.path.insert(0, str(repo))
except Exception as exc:
    note_problem("setup", exc)
    write_manifest()
    raise

for shot in SHOTS:
    out = WORKING / (shot["scene_key"] + ".mp4")
    try:
        command = [
            sys.executable, "-m", "scripts.inference",
            "--video_path", str(INPUT_DIR / shot["video"]),
            "--audio_path", str(INPUT_DIR / shot["audio"]),
            "--result_dir", str(WORKING),
            "--fps", "@@FPS@@",
        ]
        if manifest["weights_source"].startswith("dataset"):
            command += ["--model_dir", str(WEIGHTS / "musetalk")]
        subprocess.run(command, cwd=str(repo), check=True)
        if not out.exists():
            produced = sorted(WORKING.glob("*.mp4"))
            if produced:
                produced[-1].rename(out)
        if not out.exists():
            raise FileNotFoundError("the inference script produced no video")
        manifest["model_ran"] = True
        manifest["shots"].append(
            {"scene_key": shot["scene_key"], "file": out.name, "generated_motion": True,
             "requested_duration_s": shot["duration_s"]}
        )
    except Exception as exc:
        # No substitute is written here either.
        note_problem(shot["scene_key"], exc)
        manifest["shots"].append(
            {"scene_key": shot["scene_key"], "file": None, "generated_motion": False,
             "error": f"{type(exc).__name__}: {exc}"}
        )

write_manifest()
print(json.dumps(manifest, indent=2), flush=True)
'''


def render(spec: NotebookSpec) -> str:
    """Build the notebook JSON for one remote run."""
    from app.constants import RemotePurpose

    if spec.purpose not in (RemotePurpose.LIPSYNC, RemotePurpose.IMAGE_TO_VIDEO):
        raise ValueError(f"No notebook template for purpose '{spec.purpose}'.")

    shots_json = json.dumps(spec.shots)
    if '"""' in shots_json or "@@" in shots_json:
        # The shot list is embedded in a raw triple-quoted string, so
        # either sequence would break out of it.
        raise ValueError("A shot contains text that cannot be embedded in a notebook.")

    preamble = _fill(
        _PREAMBLE,
        working=WORKING_DIR,
        manifest_name=MANIFEST_NAME,
        purpose=spec.purpose,
        inputs=spec.inputs_json,
        shots=shots_json,
    )
    weights = _fill(_WEIGHTS, weights_dir=spec.weights_dir)

    if spec.purpose == RemotePurpose.LIPSYNC:
        body = _fill(_LIPSYNC_BODY, fps=spec.fps)
    else:
        body = _fill(
            _I2V_BODY, fps=spec.fps, width=spec.width, height=spec.height
        )

    return _notebook([_cell(preamble), _cell(weights), _cell(_GPU_CHECK), _cell(body)])


def parse_manifest(text: str) -> dict[str, Any]:
    """Read a run's manifest, tolerating a notebook that died mid-write.

    A manifest that cannot be read means the run's own account of itself
    is missing, which is reported rather than assumed to mean success.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {
            "readable": False,
            "model_ran": False,
            "shots": [],
            "problems": ["The run's manifest could not be read, so what it did is unknown."],
        }
    if not isinstance(data, dict):
        return {
            "readable": False,
            "model_ran": False,
            "shots": [],
            "problems": ["The run's manifest was not an object."],
        }
    data.setdefault("shots", [])
    data.setdefault("problems", [])
    data.setdefault("model_ran", False)
    data["readable"] = True
    return data
