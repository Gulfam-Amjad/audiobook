"""Prompt templates, substitution and resolution precedence."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Image
from app.services import prompts as prompt_service
from app.services.prompts import PromptError


async def _image(session: AsyncSession, name: str = "sunset-beach.jpg") -> Image:
    image = Image(
        path=str(Path("D:/pics/holiday") / name),
        filename=name,
        extension=".jpg",
        sha256="a" * 64,
        size_bytes=1234,
        width=1920,
        height=1080,
    )
    session.add(image)
    await session.commit()
    await session.refresh(image)
    return image


def test_substitute_fills_known_tokens() -> None:
    rendered, unresolved = prompt_service.substitute(
        "A {style} shot of {stem} at {width}px", {"style": "cinematic", "stem": "beach", "width": "1920"}
    )
    assert rendered == "A cinematic shot of beach at 1920px"
    assert unresolved == ()


def test_substitute_keeps_unknown_tokens_visible() -> None:
    rendered, unresolved = prompt_service.substitute("Make {subject} move", {})
    assert "{subject}" in rendered, "a typo must stay visible rather than vanish"
    assert unresolved == ("subject",)


def test_substitute_does_not_evaluate_attributes() -> None:
    """A prompt body is user data, not a format string."""
    rendered, _ = prompt_service.substitute("{x.__class__} {0} {}", {"x": "safe"})
    assert rendered == "{x.__class__} {0} {}"


async def test_create_first_template_becomes_default(session: AsyncSession) -> None:
    template = await prompt_service.create_template(
        session, name="Push in", body="Slow push in on {stem}."
    )
    await session.commit()
    assert template.is_default is True


async def test_only_one_default_at_a_time(session: AsyncSession) -> None:
    first = await prompt_service.create_template(session, name="One", body="Body one")
    second = await prompt_service.create_template(session, name="Two", body="Body two")
    await session.commit()

    await prompt_service.set_default(session, second.id)
    await session.commit()

    await session.refresh(first)
    await session.refresh(second)
    assert first.is_default is False
    assert second.is_default is True


async def test_duplicate_template_name_is_refused(session: AsyncSession) -> None:
    await prompt_service.create_template(session, name="Orbit", body="Orbit slowly")
    await session.commit()
    with pytest.raises(PromptError, match="already exists"):
        await prompt_service.create_template(session, name="Orbit", body="Something else")


async def test_empty_body_is_refused(session: AsyncSession) -> None:
    with pytest.raises(PromptError, match="prompt body"):
        await prompt_service.create_template(session, name="Blank", body="   ")


async def test_deleting_the_default_promotes_another(session: AsyncSession) -> None:
    first = await prompt_service.create_template(session, name="First", body="One")
    second = await prompt_service.create_template(session, name="Second", body="Two")
    await session.commit()
    assert first.is_default

    await prompt_service.delete_template(session, first.id)
    await session.commit()

    await session.refresh(second)
    assert second.is_default is True, "the library must never be left without a default"


async def test_resolution_uses_default_template(session: AsyncSession) -> None:
    await prompt_service.create_template(
        session, name="Default", body="Animate {stem} gently.", negative_prompt="flicker"
    )
    image = await _image(session)

    resolved = await prompt_service.resolve_for_image(session, image)

    assert resolved.source == "template"
    assert resolved.prompt == "Animate sunset-beach gently."
    assert resolved.negative_prompt == "flicker"


async def test_image_override_beats_template(session: AsyncSession) -> None:
    await prompt_service.create_template(session, name="Default", body="Template text")
    image = await _image(session)
    await prompt_service.set_image_prompt(session, image.id, "Specific motion for this one")
    await session.commit()

    resolved = await prompt_service.resolve_for_image(session, image)

    assert resolved.source == "image_override"
    assert resolved.prompt == "Specific motion for this one"


async def test_literal_beats_everything(session: AsyncSession) -> None:
    await prompt_service.create_template(session, name="Default", body="Template text")
    image = await _image(session)
    await prompt_service.set_image_prompt(session, image.id, "Override text")
    await session.commit()

    resolved = await prompt_service.resolve_for_image(
        session, image, literal_prompt="One-off prompt for {stem}"
    )

    assert resolved.source == "literal"
    assert resolved.prompt == "One-off prompt for sunset-beach"


async def test_clearing_an_override_falls_back_to_template(session: AsyncSession) -> None:
    await prompt_service.create_template(session, name="Default", body="Template text")
    image = await _image(session)
    await prompt_service.set_image_prompt(session, image.id, "Override")
    await session.commit()

    assert await prompt_service.clear_image_prompt(session, image.id) is True
    await session.commit()

    resolved = await prompt_service.resolve_for_image(session, image)
    assert resolved.source == "template"


async def test_resolution_without_any_prompt_raises(session: AsyncSession) -> None:
    image = await _image(session)
    with pytest.raises(PromptError, match="No prompt available"):
        await prompt_service.resolve_for_image(session, image)


async def test_template_variables_are_applied(session: AsyncSession) -> None:
    await prompt_service.create_template(
        session,
        name="Styled",
        body="A {style} clip of {stem}",
        variables={"style": "photoreal"},
    )
    image = await _image(session)

    resolved = await prompt_service.resolve_for_image(session, image)
    assert resolved.prompt == "A photoreal clip of sunset-beach"


async def test_image_tokens_cannot_be_shadowed_by_variables(session: AsyncSession) -> None:
    """Image facts win, so a template cannot claim the wrong filename."""
    await prompt_service.create_template(
        session, name="Shadow", body="File is {filename}", variables={"filename": "wrong.jpg"}
    )
    image = await _image(session)

    resolved = await prompt_service.resolve_for_image(session, image)
    assert resolved.prompt == "File is sunset-beach.jpg"


async def test_starter_templates_seed_once(session: AsyncSession) -> None:
    first = await prompt_service.ensure_starter_templates(session)
    await session.commit()
    second = await prompt_service.ensure_starter_templates(session)
    await session.commit()

    assert first == len(prompt_service.STARTER_TEMPLATES)
    assert second == 0
    templates = await prompt_service.list_templates(session)
    assert sum(1 for t in templates if t.is_default) == 1
