# CLAUDE.md — Project Context for AI Assistants

Give this file to Cursor / Claude / any AI chat so it understands the project before helping.

## What this project is

**AI Video Generation Manager** — a **local Windows app** (one Python process) for producing
YouTube videos: faceless documentary/story videos, YouTube Shorts, and audiobooks.

Pipeline: **brief → script → scenes/shots → images/stock footage → image-to-video (cloud or manual)
→ narration (TTS) → captions/subtitles → sound design/music → FFmpeg render → upload assets**
(title, description, tags, thumbnail). It **prepares** files for YouTube; it does **not** upload them.

Example outputs live in `mighty-chapters-shorts/` and `mighty-chapters-shorts-v2/` (mp4, srt,
cover jpg, description txt, upload plan).

## Tech stack

| Area | What is used |
| --- | --- |
| Language | Python 3.14 (Windows, `.venv`) |
| Web server | FastAPI + Uvicorn |
| UI | Server-rendered Jinja2 templates + **HTMX** + **Alpine.js** (no Node, no build step) |
| Database | SQLite via SQLAlchemy 2 (async) + aiosqlite |
| Migrations | Alembic (`migrations/`, auto-run on startup) |
| Config | pydantic-settings, `.env` file (all vars prefixed `AVM_`) |
| HTTP client | httpx (forced IPv4) |
| Video/audio | FFmpeg / FFprobe (external binaries) |
| Shot detection | PySceneDetect + OpenCV |
| TTS voices | Piper (local), Kokoro-onnx (local), edge-tts (Microsoft online, Hindi/Urdu) |
| Word timing | faster-whisper (captions) |
| Images | Pillow |
| Maps | pyshp (Natural Earth shapes) |
| Audio synthesis | numpy (effects, ambience beds) |
| File watching | watchdog (manual-assist inbox) |
| Secrets | keyring + `.env` (never rendered/logged; redactor applied) |
| Tests | pytest, pytest-asyncio, respx (HTTP mocking) |

**Not used on purpose:** Docker, Redis, Celery, Node/React, browser automation of provider sites.
Reason: laptop has 8 GB RAM, 2 GB VRAM, and very little space on C:.

## External services / providers

- **Script writing:** Google Gemini API (`AVM_GEMINI_API_KEY`) or manual paste.
- **Image-to-video adapters (`app/providers/`):** Alibaba (Wan), BytePlus (Seedance), fal.ai,
  Replicate, MiniMax/Hailuo, PixVerse, Runway, Luma, Pika, Vidu, Kling, plus `mock` and `manual`.
- **Borrowed GPU (`app/providers/compute/`):** Kaggle (preferred), Hugging Face Space (fallback).
- **Stock media:** Wikimedia Commons (no key), Pixabay (optional key). Research notes about
  Pexels/Unsplash/Coverr are in `.firecrawl/`.

## Architecture

```
start.ps1  ->  uvicorn app.main:app  (http://127.0.0.1:8765)

app/
  main.py          FastAPI factory + lifespan: runs migrations, syncs providers,
                   seeds prompts, starts the Scheduler and Inbox Watcher (same process)
  config.py        Settings (pydantic-settings, AVM_* env vars, data dirs)
  db.py            Async SQLAlchemy engine/session (session_scope)
  models.py        ORM tables (see below)
  security.py      Optional UI token
  templating.py    Jinja2 setup
  routes/          One router per screen (thin; call services, return HTML partials/JSON)
  services/        All business logic (see below)
  providers/       Adapters for video, script, speech and compute providers
    base.py, registry.py, http_adapter.py  -> common adapter interface & registry
    script/        gemini, manual_paste
    speech/        piper, kokoro, edge
    compute/       kaggle, hf_space
  templates/       Jinja pages + partials/ (HTMX swaps)
  static/          htmx.min.js, alpine.min.js, app.css
migrations/        Alembic versions
scripts/           CLI scripts that build specific videos/books (make_book.py, make_film.py,
                   make_shorts.py, make_mighty_shorts_v2.py, make_audiobook.py, one_step.py ...)
                   Many drive the running app over HTTP.
tests/             pytest suite (one file per feature)
docs/              DECISIONS, OPEN_TASKS, PROTOTYPE, PROVIDERS, ROADMAP, SETUP
```

### Key services (`app/services/`)

- **Production:** `projects`, `scripts`, `scene_plan`, `directing`, `camera`, `film`, `production`
- **Media:** `images`, `library`, `stock_media`, `graphics`, `maps`, `faces`, `shot_detect`
- **Generation & queue:** `jobs`, `scheduler` (DB-backed state machine), `generation`, `routing`
  (picks provider), `selection`, `remote` (Kaggle/HF), `downloads`
- **Money/credits:** `credits`, `spending`, `accounts`, `capacity`
- **Audio:** `speech`, `piper`, `audiobook`, `mixing`, `sound_design`, `sound_library`, `sound_synth`
- **Finishing:** `render`, `assembly`, `ffmpeg`, `captions`, `subtitles`, `transitions`, `publishing`
- **Books:** `book_text`, `book_package`, `audiobook`
- **Manual flow:** `manual_assist`, `inbox_watcher`
- **Misc:** `settings_store`, `paths`, `files`, `stats`, `notebooks`, `migrate`, `timeutil`

### Database tables (`app/models.py`)

Setting, ImageFolder, Image, PromptTemplate, ImagePrompt, Provider, Account, Entitlement, Batch,
Job, JobEvent, Output, InboxItem, Assembly, SpendEntry, VoiceProfile, Project, Character, Script,
Scene, AudioSegment, RemoteTask, SceneClip, Render, StageCheckpoint.

### UI screens

The sidebar is grouped (`NAV_GROUPS` in `app/routes/common.py`):

- **Books (local):** Book Video, Audiobook, Voices, Files.
- **Video Projects:** Projects (with Script, Shots, Narration, Render sub-screens), Image Library,
 Prompts, Assembly, Video Library.
- **Cloud & Remote GPU** (earlier idea, folded by default): Overview, Providers, Credits, Queue,
 Manual Assist, Remote GPU, Daily Capacity.
- **System:** Settings, Diagnostics.

API docs at `/api/docs`, health at `/health`.

## Rules the code enforces (do not break these)

1. **No spending by default.** Paid generation needs all three: `AVM_PAID_ENABLED=true`, monthly cap
   > $0, and per-account authorisation.
2. **No browser automation** of provider websites and no multi-account trial abuse. Use the
   manual worksheet + inbox folder instead.
3. A submit that times out is `uncertain` — never auto-retried until reconciled.
4. Balances are `api_verified`, `user_entered`, or `unknown` — unknown is never stored as 0.
5. A provider is `verified` only after a real video file was downloaded.
6. Durations come from FFprobe; assembled length is not counted as generated AI video.
7. Secrets stay in `.env`; never render or log them.
8. HTTP clients bind IPv4 (`0.0.0.0`) — IPv6 hangs on this network.

## Machine constraints

- Windows 10, PowerShell, 8 GB RAM, 2 GB VRAM (no local video models).
- C: drive almost full — data lives on **D:** (`AVM_DATA_ROOT=D:\AIVideoManager`: DB, thumbnails,
  outputs, inbox, logs).
- FFmpeg 9 installed; Piper + `en_US-joe-medium` installed on D:.

## How to run

```powershell
.\start.ps1              # creates .venv, installs requirements, copies .env, starts server
.\start.ps1 -Reload      # dev auto-reload
.\.venv\Scripts\python -m pytest     # run tests
python scripts/make_book.py --gutenberg 1661 --voice kokoro:bm_george   # example CLI build
```

## Coding conventions

- Keep routes thin; logic goes in `app/services/`.
- New provider = new adapter in `app/providers/` implementing `base.py`, register in `registry.py`.
- UI changes = Jinja template + HTMX partial in `templates/partials/`; no JS frameworks.
- Schema change = new Alembic migration in `migrations/versions/`.
- Add/adjust a test in `tests/` for every feature (mock HTTP with respx).

## Where to look for more

`README.md`, `docs/DECISIONS.md`, `docs/OPEN_TASKS.md`, `docs/PROVIDERS.md`, `docs/ROADMAP.md`,
`docs/SETUP.md`, `docs/PROTOTYPE.md`.
