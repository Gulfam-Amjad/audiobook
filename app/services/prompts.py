"""Prompt templates and per-image prompt resolution.

Substitution is done with an explicit token walker rather than
``str.format`` or Jinja, because prompt bodies are user data: ``format``
would expose attribute access (``{x.__class__}``) and a template engine would
be a code-execution surface for no benefit. Unknown tokens are left intact so
a typo is visible in the prompt rather than silently blanked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Image, ImagePrompt, PromptTemplate

TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]{1,40})\}")

#: Tokens derived from the image itself, always available.
BUILTIN_TOKENS = ("filename", "stem", "extension", "width", "height", "folder")


class PromptError(ValueError):
    """Raised when no usable prompt can be produced."""


@dataclass
class ResolvedPrompt:
    prompt: str
    negative_prompt: str | None
    source: str  # "image_override" | "template" | "literal"
    template_name: str | None = None
    unresolved_tokens: tuple[str, ...] = ()


def image_tokens(image: Image) -> dict[str, str]:
    path = Path(image.path)
    return {
        "filename": image.filename,
        "stem": path.stem,
        "extension": image.extension.lstrip("."),
        "width": str(image.width) if image.width else "",
        "height": str(image.height) if image.height else "",
        "folder": path.parent.name,
    }


def substitute(body: str, values: dict[str, str]) -> tuple[str, tuple[str, ...]]:
    """Replace ``{token}`` occurrences, reporting any that had no value."""
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values and values[name] != "":
            return values[name]
        unresolved.append(name)
        return match.group(0)

    rendered = TOKEN_RE.sub(replace, body)
    # Collapse the whitespace that empty substitutions tend to leave behind.
    rendered = re.sub(r"[ \t]{2,}", " ", rendered).strip()
    return rendered, tuple(dict.fromkeys(unresolved))


def find_tokens(body: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(TOKEN_RE.findall(body or "")))


# ---------------------------------------------------------------------------
# Template management
# ---------------------------------------------------------------------------


async def list_templates(session: AsyncSession) -> list[PromptTemplate]:
    return list(
        (
            await session.execute(
                select(PromptTemplate).order_by(
                    PromptTemplate.is_default.desc(), PromptTemplate.name
                )
            )
        )
        .scalars()
        .all()
    )


async def get_default_template(session: AsyncSession) -> PromptTemplate | None:
    return (
        await session.execute(
            select(PromptTemplate).where(PromptTemplate.is_default.is_(True)).limit(1)
        )
    ).scalar_one_or_none()


async def set_default(session: AsyncSession, template_id: int) -> PromptTemplate:
    template = await session.get(PromptTemplate, template_id)
    if template is None:
        raise PromptError("Template not found.")
    for other in await list_templates(session):
        other.is_default = other.id == template_id
    await session.flush()
    return template


async def create_template(
    session: AsyncSession,
    *,
    name: str,
    body: str,
    negative_prompt: str | None = None,
    variables: dict[str, str] | None = None,
    make_default: bool = False,
) -> PromptTemplate:
    name = (name or "").strip()
    body = (body or "").strip()
    if not name:
        raise PromptError("A template needs a name.")
    if not body:
        raise PromptError("A template needs a prompt body.")

    clash = (
        await session.execute(select(PromptTemplate).where(PromptTemplate.name == name))
    ).scalar_one_or_none()
    if clash is not None:
        raise PromptError(f"A template called '{name}' already exists.")

    template = PromptTemplate(
        name=name,
        body=body,
        negative_prompt=(negative_prompt or "").strip() or None,
        variables=variables or {},
    )
    session.add(template)
    await session.flush()

    # The first template saved becomes the default, so the queue always has
    # something to fall back on.
    if make_default or not await _has_default(session):
        await set_default(session, template.id)
    return template


async def update_template(
    session: AsyncSession,
    template_id: int,
    *,
    name: str,
    body: str,
    negative_prompt: str | None = None,
    variables: dict[str, str] | None = None,
) -> PromptTemplate:
    template = await session.get(PromptTemplate, template_id)
    if template is None:
        raise PromptError("Template not found.")
    name = (name or "").strip()
    body = (body or "").strip()
    if not name or not body:
        raise PromptError("Both a name and a prompt body are required.")

    clash = (
        await session.execute(
            select(PromptTemplate).where(
                PromptTemplate.name == name, PromptTemplate.id != template_id
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise PromptError(f"A template called '{name}' already exists.")

    template.name = name
    template.body = body
    template.negative_prompt = (negative_prompt or "").strip() or None
    if variables is not None:
        template.variables = variables
    await session.flush()
    return template


async def delete_template(session: AsyncSession, template_id: int) -> str:
    template = await session.get(PromptTemplate, template_id)
    if template is None:
        raise PromptError("Template not found.")
    name = template.name
    was_default = template.is_default
    await session.delete(template)
    await session.flush()

    if was_default:
        remaining = await list_templates(session)
        if remaining:
            await set_default(session, remaining[0].id)
    return name


async def _has_default(session: AsyncSession) -> bool:
    return await get_default_template(session) is not None


# ---------------------------------------------------------------------------
# Per-image overrides
# ---------------------------------------------------------------------------


async def set_image_prompt(
    session: AsyncSession, image_id: int, prompt: str, negative_prompt: str | None = None
) -> ImagePrompt:
    prompt = (prompt or "").strip()
    if not prompt:
        raise PromptError("A per-image prompt cannot be empty. Delete it instead.")

    row = (
        await session.execute(select(ImagePrompt).where(ImagePrompt.image_id == image_id))
    ).scalar_one_or_none()
    if row is None:
        row = ImagePrompt(image_id=image_id)
        session.add(row)
    row.prompt = prompt
    row.negative_prompt = (negative_prompt or "").strip() or None
    await session.flush()
    return row


async def clear_image_prompt(session: AsyncSession, image_id: int) -> bool:
    row = (
        await session.execute(select(ImagePrompt).where(ImagePrompt.image_id == image_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def get_image_prompt(session: AsyncSession, image_id: int) -> ImagePrompt | None:
    return (
        await session.execute(select(ImagePrompt).where(ImagePrompt.image_id == image_id))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_for_image(
    session: AsyncSession,
    image: Image,
    *,
    template_id: int | None = None,
    literal_prompt: str | None = None,
    literal_negative: str | None = None,
) -> ResolvedPrompt:
    """Decide the prompt for one image.

    Precedence: an explicit literal passed by the caller, then a per-image
    override, then the named or default template.
    """
    tokens = image_tokens(image)

    if literal_prompt and literal_prompt.strip():
        rendered, unresolved = substitute(literal_prompt.strip(), tokens)
        return ResolvedPrompt(
            prompt=rendered,
            negative_prompt=(literal_negative or "").strip() or None,
            source="literal",
            unresolved_tokens=unresolved,
        )

    override = await get_image_prompt(session, image.id)
    if override is not None:
        rendered, unresolved = substitute(override.prompt, tokens)
        return ResolvedPrompt(
            prompt=rendered,
            negative_prompt=override.negative_prompt,
            source="image_override",
            unresolved_tokens=unresolved,
        )

    template = (
        await session.get(PromptTemplate, template_id)
        if template_id
        else await get_default_template(session)
    )
    if template is None:
        raise PromptError(
            "No prompt available. Create a prompt template and mark it as the default, "
            "or set a prompt on this image."
        )

    values = dict(template.variables or {})
    values.update(tokens)
    rendered, unresolved = substitute(template.body, values)
    if not rendered:
        raise PromptError(f"Template '{template.name}' renders to an empty prompt.")

    return ResolvedPrompt(
        prompt=rendered,
        negative_prompt=template.negative_prompt,
        source="template",
        template_name=template.name,
        unresolved_tokens=unresolved,
    )


def preview(template: PromptTemplate, sample: dict[str, str] | None = None) -> str:
    """Render a template with placeholder-ish sample values for the editor."""
    values = dict(template.variables or {})
    values.update(
        {
            "filename": "example-photo.jpg",
            "stem": "example-photo",
            "extension": "jpg",
            "width": "1920",
            "height": "1080",
            "folder": "source-set",
        }
    )
    values.update(sample or {})
    rendered, _ = substitute(template.body, values)
    return rendered


#: Shipped so a new install has a sensible, honest starting point.
STARTER_TEMPLATES: tuple[dict[str, object], ...] = (
    {
        "name": "Subtle camera push",
        "body": (
            "Slow cinematic camera push-in on the subject. Natural, gentle motion. "
            "Keep the composition, lighting and colours of the source image unchanged. "
            "No new objects, no text, no morphing."
        ),
        "negative_prompt": "warping, distorted faces, flicker, text, watermark, extra limbs",
        "make_default": True,
    },
    {
        "name": "Ambient scene motion",
        "body": (
            "Bring the scene to life with ambient motion only: drifting clouds, moving water, "
            "swaying foliage and shifting light. The camera stays still. "
            "Preserve the subject exactly as photographed."
        ),
        "negative_prompt": "camera shake, zoom, morphing, text, watermark",
    },
    {
        "name": "Slow orbit",
        "body": (
            "Slow parallax orbit around the subject, revealing depth. "
            "Steady pace, consistent lighting, photoreal. "
            "Do not change the subject's appearance."
        ),
        "negative_prompt": "fast motion, jitter, duplicated subject, text, watermark",
    },
)


async def ensure_starter_templates(session: AsyncSession) -> int:
    """Seed example templates on an empty install. Returns how many were added."""
    if await list_templates(session):
        return 0
    added = 0
    for spec in STARTER_TEMPLATES:
        await create_template(
            session,
            name=str(spec["name"]),
            body=str(spec["body"]),
            negative_prompt=str(spec.get("negative_prompt") or "") or None,
            make_default=bool(spec.get("make_default")),
        )
        added += 1
    return added
