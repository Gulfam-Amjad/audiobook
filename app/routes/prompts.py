"""Prompt template management."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Image
from app.routes.common import fragment, page
from app.security import require_local_access
from app.services import prompts as prompt_service
from app.services.prompts import PromptError

router = APIRouter()


def _parse_variables(raw: str) -> dict[str, str]:
    """Accept either JSON or simple ``key=value`` lines."""
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"Variables are not valid JSON: {exc.msg}"
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="Variables must be a JSON object.")
        return {str(k): str(v) for k, v in parsed.items()}

    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise HTTPException(
                status_code=400,
                detail=f"Could not read variable line '{line.strip()}'. Use key=value.",
            )
        values[key.strip()] = value.strip()
    return values


async def _template_context(session: AsyncSession) -> dict[str, object]:
    templates = await prompt_service.list_templates(session)
    return {
        "templates": templates,
        "previews": {t.id: prompt_service.preview(t) for t in templates},
        "builtin_tokens": prompt_service.BUILTIN_TOKENS,
    }


@router.get("/prompts", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    return page(request, "prompts.html", await _template_context(session))


@router.get("/partials/prompts/list", response_class=HTMLResponse, include_in_schema=False)
async def template_list(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    return fragment(request, "partials/prompt_list.html", await _template_context(session))


@router.post(
    "/prompts",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def create(
    request: Request,
    name: str = Form(...),
    body: str = Form(...),
    negative_prompt: str = Form(""),
    variables: str = Form(""),
    make_default: bool = Form(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        template = await prompt_service.create_template(
            session,
            name=name,
            body=body,
            negative_prompt=negative_prompt,
            variables=_parse_variables(variables),
            make_default=make_default,
        )
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/prompt_list.html",
        await _template_context(session),
        flash=f"Saved template '{template.name}'.",
    )


# Declared before "/prompts/{template_id}" so the literal path wins the match.
@router.post(
    "/prompts/seed-starters",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def seed_starters(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    added = await prompt_service.ensure_starter_templates(session)
    await session.commit()
    message = (
        f"Added {added} starter template(s)."
        if added
        else "Templates already exist, so nothing was added."
    )
    return fragment(
        request, "partials/prompt_list.html", await _template_context(session), flash=message
    )


@router.post(
    "/prompts/{template_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def update(
    request: Request,
    template_id: int,
    name: str = Form(...),
    body: str = Form(...),
    negative_prompt: str = Form(""),
    variables: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    try:
        template = await prompt_service.update_template(
            session,
            template_id,
            name=name,
            body=body,
            negative_prompt=negative_prompt,
            variables=_parse_variables(variables),
        )
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/prompt_list.html",
        await _template_context(session),
        flash=f"Updated '{template.name}'.",
    )


@router.post(
    "/prompts/{template_id}/default",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def make_default(
    request: Request, template_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        template = await prompt_service.set_default(session, template_id)
    except PromptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/prompt_list.html",
        await _template_context(session),
        flash=f"'{template.name}' is now the default prompt.",
    )


@router.delete(
    "/prompts/{template_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def delete(
    request: Request, template_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    try:
        name = await prompt_service.delete_template(session, template_id)
    except PromptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return fragment(
        request,
        "partials/prompt_list.html",
        await _template_context(session),
        flash=f"Deleted '{name}'.",
    )


# ---------------------------------------------------------------------------
# Per-image overrides
# ---------------------------------------------------------------------------


@router.get(
    "/images/{image_id}/prompt", response_class=HTMLResponse, include_in_schema=False
)
async def image_prompt_form(
    request: Request, image_id: int, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    image = await session.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found.")

    return fragment(
        request,
        "partials/image_prompt_form.html",
        await _image_prompt_context(session, image),
    )


@router.post(
    "/images/{image_id}/prompt",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_local_access)],
)
async def save_image_prompt(
    request: Request,
    image_id: int,
    prompt: str = Form(""),
    negative_prompt: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    image = await session.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found.")

    if not prompt.strip():
        cleared = await prompt_service.clear_image_prompt(session, image_id)
        await session.commit()
        message = (
            "Per-image prompt removed; this image now uses the default template."
            if cleared
            else "No per-image prompt was set."
        )
    else:
        try:
            await prompt_service.set_image_prompt(session, image_id, prompt, negative_prompt)
        except PromptError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await session.commit()
        message = f"Prompt saved for {image.filename}."

    return fragment(
        request,
        "partials/image_prompt_form.html",
        await _image_prompt_context(session, image),
        flash=message,
    )


async def _image_prompt_context(session: AsyncSession, image: Image) -> dict[str, object]:
    override = await prompt_service.get_image_prompt(session, image.id)
    try:
        resolved = await prompt_service.resolve_for_image(session, image)
        effective, source = resolved.prompt, resolved.source
    except PromptError as exc:
        effective, source = str(exc), "none"
    return {
        "image": image,
        "override": override,
        "effective": effective,
        "effective_source": source,
        "tokens": prompt_service.image_tokens(image),
    }
