"""The Book Video page: a script in, an upload-ready YouTube video out.

Paste or upload a script, choose a cover picture, a template and a voice
(each voice has a ten-second demo), check the estimate, then make a
fifteen-second preview or the whole video. Both go through the overnight
queue (:mod:`app.services.book_queue`), so the work carries on after the
page, the server or the PC is closed. The jobs list shows progress, and a
finished job shows its upload kit.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Image
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import book_queue
from app.services import book_studio as studio
from app.services import book_text
from app.services import video_templates
from app.services import visualizer
from app.services import voice_catalog
from app.services.book_studio import StudioError

router = APIRouter()
JOBS = "partials/book_video_jobs.html"
CHECK = "partials/book_video_check.html"
COVER = "partials/book_video_cover.html"
PICKS = "partials/book_video_picks.html"
VOICE_STATUS = "partials/book_video_voice_status.html"
LIBRARY_LIMIT = 48
#: Starlette refuses a form field over 1024 KB unless the route asks for more.
#: A full novel, after the browser encodes it, is larger than that. The studio
#: still rejects a script over its own 8 MB cap.
_FORM_PART_BYTES = max(studio.MAX_SCRIPT_BYTES, studio.MAX_COVER_BYTES)


async def _posted(request: Request):
    """The form, with room for a full book script or a cover picture."""
    return await request.form(max_part_size=_FORM_PART_BYTES)


def _field(form, name: str, default: str = "") -> str:
    value = form.get(name)
    return value if isinstance(value, str) else default


def _field_int(form, name: str, default: int) -> int:
    try:
        return int(_field(form, name, str(default)))
    except ValueError:
        return default


def _field_float(form, name: str, default: float) -> float:
    try:
        return float(_field(form, name, str(default)))
    except ValueError:
        return default
_demo_task: asyncio.Task | None = None


def _jobs_context() -> dict:
    book_queue.park_orphans()
    rows = studio.job_rows()
    return {"jobs": rows, "summary": studio.queue_summary(rows), "disk": studio.disk_status()}


def _cover_context(path: Path | None, note: str = "") -> dict:
    return {"cover_rel": studio.cover_rel(path) if path else "", "cover_name": path.name if path else "",
            "cover_credit": studio.cover_credit(path), "note": note}


def _cover_response(request: Request, path: Path | None, note: str = "") -> HTMLResponse:
    context = _cover_context(path, note)
    response = fragment(request, COVER, context)
    response.headers["HX-Trigger"] = json.dumps({"cover-changed": {"rel": context["cover_rel"]}})
    return response


@router.get("/book-video", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    saved = studio.load_settings()
    templates = [{
        "id": t.id, "name": t.name, "genres": t.genres, "blurb": t.blurb, "ring": t.ring,
        "accent": t.accent_hex, "swatch": t.swatch,
    } for t in video_templates.TEMPLATES]
    return page(request, "book_video.html", {
        "saved": saved, "templates": templates, "voices": voice_catalog.rows(),
        "voice_status": voice_catalog.status(), "kinds": book_text.KINDS, "languages": book_text.LANGUAGES,
        "styles": visualizer.STYLES, "language_names": voice_catalog.LANGUAGE_NAMES,
        "missing_fonts": video_templates.missing_fonts(), **_cover_context(None), **_jobs_context(),
    })


@router.get("/partials/book-video/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs(request: Request) -> HTMLResponse:
    return fragment(request, JOBS, _jobs_context())


@router.get("/partials/queue-strip", response_class=HTMLResponse, include_in_schema=False)
async def queue_strip(request: Request) -> HTMLResponse:
    book_queue.park_orphans()
    return fragment(request, "partials/queue_strip.html", {"status": studio.queue_status()})


@router.post("/book-video/check", response_class=HTMLResponse, include_in_schema=False)
async def check(request: Request) -> HTMLResponse:
    form = await _posted(request)
    try:
        result = await asyncio.to_thread(
            studio.check_script, _field(form, "script"), title=_field(form, "title"),
            author=_field(form, "author"), kind=_field(form, "kind", "summary"),
            language=_field(form, "language", "en"), voice=_field(form, "voice"),
            template=_field(form, "template"), height=_field_int(form, "height", 1080),
            video=_field(form, "video", "animated"),
            channel=_field(form, "channel") or studio.DEFAULTS["channel"],
        )
    finally:
        await form.close()
    return fragment(request, CHECK, {"check": result})


# --- Covers ---------------------------------------------------------------------------------


@router.post("/book-video/cover/upload", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def cover_upload(request: Request, cover_file: UploadFile = File(...)) -> HTMLResponse:
    data = await cover_file.read(studio.MAX_COVER_BYTES + 1)
    try:
        path = await asyncio.to_thread(studio.save_cover, data, cover_file.filename or "")
    except StudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _cover_response(request, path)


@router.get("/partials/book-video/library", response_class=HTMLResponse, include_in_schema=False)
async def cover_library(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    images = (await session.execute(
        select(Image).where(Image.thumb_path.is_not(None)).order_by(Image.id.desc()).limit(LIBRARY_LIMIT)
    )).scalars().all()
    rows = [{"id": i.id, "name": i.filename, "thumb": f"/images/{i.id}/thumb"} for i in images]
    return fragment(request, PICKS, {"source": "library", "rows": rows})


@router.post("/book-video/cover/library", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def cover_from_library(request: Request, image_id: int = Form(...),
                             session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    image = await session.get(Image, image_id)
    if image is None or not Path(image.path).is_file():
        raise HTTPException(status_code=404, detail="That picture is no longer on disk.")
    try:
        data = await asyncio.to_thread(Path(image.path).read_bytes)
        path = await asyncio.to_thread(studio.save_cover, data, image.filename)
    except (OSError, StudioError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _cover_response(request, path)


@router.post("/book-video/cover/search", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def cover_search(request: Request, query: str = Form("")) -> HTMLResponse:
    try:
        rows = await studio.commons_search(query)
    except StudioError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return fragment(request, PICKS, {"source": "commons", "rows": rows, "query": query})


@router.post("/book-video/cover/commons", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def cover_from_commons(request: Request, url: str = Form(...), credit: str = Form("")) -> HTMLResponse:
    try:
        path = await studio.commons_fetch(url, credit)
    except StudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _cover_response(request, path, note="Credit is added to the description.")


@router.post("/book-video/cover/clear", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def cover_clear(request: Request) -> HTMLResponse:
    return _cover_response(request, None)


@router.get("/book-video/template-preview/{template_id}", include_in_schema=False)
async def template_preview(template_id: str, cover: str = "") -> FileResponse:
    if template_id not in video_templates.BY_ID:
        raise HTTPException(status_code=404, detail="No such template.")
    try:
        art = studio.cover_from(cover) if cover else None
    except StudioError:
        art = None
    path = await asyncio.to_thread(studio.template_preview, template_id, art)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# --- Voices -------------------------------------------------------------------------------------


@router.get("/book-video/voice-demo/{name}", include_in_schema=False)
async def voice_demo(name: str) -> FileResponse:
    folder = voice_catalog.demo_dir().resolve()
    path = (folder / name).resolve()
    if path.parent != folder or path.suffix != ".mp3" or not path.is_file():
        raise HTTPException(status_code=404, detail="That demo has not been made yet.")
    return FileResponse(path, media_type="audio/mpeg")


@router.get("/partials/book-video/voice-status", response_class=HTMLResponse, include_in_schema=False)
async def voice_status(request: Request) -> HTMLResponse:
    return fragment(request, VOICE_STATUS, {"voice_status": voice_catalog.status()})


@router.post("/book-video/voice-demos", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def make_demos(request: Request) -> HTMLResponse:
    global _demo_task
    if _demo_task is None or _demo_task.done():
        _demo_task = asyncio.create_task(voice_catalog.make_missing())
        await asyncio.sleep(0)
    return fragment(request, VOICE_STATUS, {"voice_status": {**voice_catalog.status(), "running": True}})


# --- Making the video ---------------------------------------------------------------------------


@router.post("/book-video/create", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def create(request: Request) -> HTMLResponse:
    form = await _posted(request)
    try:
        order = studio.Order(
            script=_field(form, "script"), title=_field(form, "title"), author=_field(form, "author"),
            kind=_field(form, "kind", "summary"), language=_field(form, "language", "en"),
            voice=_field(form, "voice"), speed=_field_float(form, "speed", 1.0),
            template=_field(form, "template") or video_templates.DEFAULT_ID,
            style=_field(form, "style"), accent=_field(form, "accent"),
            height=_field_int(form, "height", 1080), video=_field(form, "video", "animated"),
            channel=_field(form, "channel") or studio.DEFAULTS["channel"],
            cover=_field(form, "cover"), art_query=_field(form, "art_query"), about=_field(form, "about"),
            name=_field(form, "name"), preview=_field(form, "preview") == "1",
        )
    finally:
        await form.close()
    try:
        result = await asyncio.to_thread(studio.queue_book, order)
    except (StudioError, book_queue.QueueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    what = "A 15-second preview" if order.preview else f"'{order.title.strip()}'"
    return fragment(request, JOBS, _jobs_context(), flash=f"{what} is in the queue ({result['job']}).")


@router.post("/book-video/jobs/{job_id}/{action}", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def job_action(request: Request, job_id: str, action: str) -> HTMLResponse:
    try:
        if action == "cancel":
            await asyncio.to_thread(book_queue.cancel, job_id)
            flash = "Cancelled. Finished chapters and segments are kept; Resume carries on."
        elif action == "resume":
            await asyncio.to_thread(book_queue.resume, job_id)
            book_queue.ensure_runner()
            flash = "Back in the queue; it carries on from its finished work."
        elif action == "remove":
            await asyncio.to_thread(book_queue.remove, job_id)
            flash = "Removed from the list. The book's folder is untouched."
        elif action == "clean":
            freed = await asyncio.to_thread(studio.clean, job_id)
            flash = f"Freed {freed / 1e6:,.0f} MB of working files."
        else:
            raise HTTPException(status_code=404, detail="Unknown action.")
    except (StudioError, book_queue.QueueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fragment(request, JOBS, _jobs_context(), flash=flash)


@router.post("/book-video/clean", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def clean_all(request: Request) -> HTMLResponse:
    freed = await asyncio.to_thread(studio.clean)
    return fragment(request, JOBS, _jobs_context(), flash=f"Freed {freed / 1e9:,.2f} GB of working files.")


@router.post("/book-video/runner", response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(require_local_access)])
async def start_runner(request: Request) -> HTMLResponse:
    parked = book_queue.park_orphans()
    started = book_queue.ensure_runner()
    if started:
        flash = "Continuing from the last finished chapter." if parked else "The queue runner started."
    else:
        flash = "The queue is already running, or empty."
    return fragment(request, JOBS, _jobs_context(), flash=flash)
