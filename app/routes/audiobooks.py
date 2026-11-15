"""The Audiobook page: paste a script, pick a voice, get an MP3.

The work happens in a background task (see :mod:`app.services.audiobook`),
so a book of several hours never holds a request open. The list of books
polls while one is being made.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import audiobook
from app.services import speech as speech_service
from app.services import voice_catalog
from app.services.audiobook import AudiobookError
from app.services.speech import SpeechError

router = APIRouter()
LIST = "partials/audiobook_list.html"
#: A ten-hour book is under a megabyte of text, even in Hindi or Urdu.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


def _list_context() -> dict:
    books = audiobook.list_books()
    running = next((book for book in books if book.get("is_running")), None)
    return {"books": books, "running": running}


async def _page_context(session: AsyncSession) -> dict:
    voices = audiobook.voices_for_page(await speech_service.list_voices(session))
    languages: list[dict[str, str]] = []
    for voice in voices:
        if voice["language"] not in {row["code"] for row in languages}:
            languages.append({"code": voice["language"], "label": voice["language_label"]})
    return {
        "voices": voices,
        "languages": languages,
        "audiobooks_dir": str(audiobook.audiobooks_dir()),
        "max_chapter_chars": audiobook.MAX_CHAPTER_CHARS,
        "voice_status": voice_catalog.status(),
        **_list_context(),
    }


@router.get("/audiobooks", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "audiobooks.html", await _page_context(session))


@router.get("/partials/audiobooks", response_class=HTMLResponse, include_in_schema=False)
async def book_list(request: Request) -> HTMLResponse:
    return fragment(request, LIST, _list_context())


@router.post(
    "/audiobooks",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def create(
    request: Request,
    title: str = Form(""),
    script: str = Form(""),
    voice_id: int = Form(...),
    speed: float = Form(1.0),
    script_file: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    text = script
    if script_file is not None and script_file.filename:
        raw = await script_file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="That file is larger than 8 MB of text.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="The file is not UTF-8 text. Save it as UTF-8 and upload it again.",
            ) from exc
        if not title.strip():
            title = script_file.filename.rsplit(".", 1)[0]
    try:
        profile = await speech_service.get_voice(session, voice_id)
        book_id = await audiobook.create_book(title, text, profile, speed)
    except (AudiobookError, SpeechError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fragment(request, LIST, _list_context(), flash=f"Started '{book_id}'.")


@router.post(
    "/audiobooks/cancel",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def cancel(request: Request) -> HTMLResponse:
    book_id = audiobook.cancel_current()
    flash = (
        f"Stopping '{book_id}'. Finished chapters are kept for Resume."
        if book_id
        else "Nothing is being made right now."
    )
    return fragment(request, LIST, _list_context(), flash=flash)


@router.post(
    "/audiobooks/{book_id}/resume",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def resume(request: Request, book_id: str) -> HTMLResponse:
    try:
        audiobook.resume(book_id)
    except AudiobookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fragment(request, LIST, _list_context(), flash=f"Resumed '{book_id}'.")


@router.post(
    "/audiobooks/{book_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def delete(request: Request, book_id: str) -> HTMLResponse:
    try:
        title = audiobook.delete_book(book_id)
    except AudiobookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return fragment(request, LIST, _list_context(), flash=f"Deleted '{title}'.")
