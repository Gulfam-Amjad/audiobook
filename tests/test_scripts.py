"""Script generation, scene-plan validation and the approval gate.

Every Gemini call here is mocked with respx. No request leaves the machine
and no quota is consumed, so nothing in this file proves the adapter works
against the real API.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ErrorClass, PipelineStage, StageStatus
from app.models import Character, Image, Scene, Script
from app.providers.script import gemini as gemini_module
from app.providers.script.base import ImageHint, ScriptRequest
from app.providers.script.gemini import GeminiScriptProvider
from app.providers.script.manual_paste import ManualPasteScriptProvider
from app.providers.script.base import ScriptProviderError
from app.services import projects as project_service
from app.services import scene_plan
from app.services import scripts as script_service
from app.services.scripts import ScriptError
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")

GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.8-flash:generateContent"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def good_scene(index: int, **overrides: Any) -> dict[str, Any]:
    scene = {
        "scene_key": f"s{index:02d}",
        "purpose": f"Beat {index}",
        "target_duration_s": 10.0,
        "narration": f"The {index}th thing that happened, described plainly.",
        "dialogue": [],
        "characters": [],
        "image_hint": "",
        "action": "She turns.",
        "expression": "wary",
        "camera": "slow push in",
        "environment": "a cold kitchen",
        "sfx": "",
        "transition": "cut",
        "preferred_method": "camera_move",
    }
    scene.update(overrides)
    return scene


def good_plan(scene_count: int = 6, **overrides: Any) -> dict[str, Any]:
    plan = {
        "title": "The Kettle",
        "logline": "A woman learns what the noise in the wall is.",
        "synopsis": "Beginning, middle and end.",
        "scenes": [good_scene(i + 1) for i in range(scene_count)],
    }
    plan.update(overrides)
    return plan


def gemini_response(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": json.dumps(plan)}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 1800},
    }


async def make_project(session: AsyncSession, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "name": "Test video",
        "target_duration_s": 60,
        "speaking_rate_wpm": 150,
        "max_generated_motion_s": 30,
    }
    values.update(overrides)
    project = await project_service.create_project(session, **values)
    await session.commit()
    return project.id


async def add_image(session: AsyncSession, tmp_path, name: str) -> Image:
    path = write_image(tmp_path / "stills" / name)
    image = Image(
        path=str(path),
        filename=name,
        extension="jpg",
        sha256=name.ljust(64, "0")[:64],
        size_bytes=path.stat().st_size,
        width=1280,
        height=720,
    )
    session.add(image)
    await session.flush()
    return image


def request_for(**overrides: Any) -> ScriptRequest:
    values: dict[str, Any] = {
        "project_name": "P",
        "niche": "folklore",
        "audience": "adults",
        "story_style": "quiet",
        "language": "en",
        "brief": "",
        "target_duration_s": 60,
        "speaking_rate_wpm": 150,
        "scene_target_s": 8.0,
        "max_scenes": 12,
        "motion_budget_s": 30.0,
    }
    values.update(overrides)
    return ScriptRequest(**values)


# ---------------------------------------------------------------------------
# Scene plan validation
# ---------------------------------------------------------------------------


def validate(plan: dict[str, Any], **overrides: Any) -> scene_plan.ValidationReport:
    kwargs: dict[str, Any] = {
        "target_duration_s": 60,
        "speaking_rate_wpm": 150,
        "motion_budget_s": 30.0,
    }
    kwargs.update(overrides)
    return scene_plan.validate(plan, **kwargs)


def test_a_sound_plan_passes() -> None:
    report = validate(good_plan())
    assert report.ok, report.errors
    assert report.scene_count == 6
    assert report.planned_duration_s == 60.0


def test_a_plan_with_no_scenes_fails() -> None:
    report = validate({"title": "T", "scenes": []})
    assert not report.ok
    assert any("no scenes" in e for e in report.errors)


def test_a_single_scene_is_too_few() -> None:
    plan = good_plan(1)
    plan["scenes"][0]["target_duration_s"] = 60.0
    report = validate(plan)
    assert any("at least" in e for e in report.errors)


def test_a_missing_title_fails() -> None:
    report = validate(good_plan(6, title=""))
    assert any("no title" in e for e in report.errors)


def test_duplicate_scene_keys_fail() -> None:
    plan = good_plan(6)
    plan["scenes"][3]["scene_key"] = plan["scenes"][0]["scene_key"]
    report = validate(plan)
    assert any("reuses the scene_key" in e for e in report.errors)


def test_a_silent_scene_fails() -> None:
    """Every shot must carry speech, or the edit has a dead hole in it."""
    plan = good_plan(6)
    plan["scenes"][2]["narration"] = None
    plan["scenes"][2]["dialogue"] = []
    report = validate(plan)
    assert any("neither narration nor dialogue" in e for e in report.errors)


def test_repeated_narration_fails() -> None:
    """Repetition is how mass-produced channels pad length."""
    plan = good_plan(6)
    plan["scenes"][4]["narration"] = plan["scenes"][1]["narration"]
    report = validate(plan)
    assert any("repeats the narration" in e for e in report.errors)


def test_repeated_narration_is_caught_regardless_of_case() -> None:
    plan = good_plan(6)
    plan["scenes"][4]["narration"] = plan["scenes"][1]["narration"].upper()
    report = validate(plan)
    assert any("repeats the narration" in e for e in report.errors)


def test_a_shot_outside_the_length_limits_fails() -> None:
    plan = good_plan(6)
    plan["scenes"][0]["target_duration_s"] = 45.0
    report = validate(plan)
    assert any("outside the" in e for e in report.errors)


def test_a_speaker_not_in_the_shot_fails() -> None:
    plan = good_plan(6)
    plan["scenes"][1]["characters"] = ["Mara"]
    plan["scenes"][1]["dialogue"] = [{"character": "Idris", "line": "Who is there?"}]
    report = validate(plan)
    assert any("not listed" in e for e in report.errors)


def test_a_speaker_in_the_shot_is_accepted() -> None:
    plan = good_plan(6)
    plan["scenes"][1]["characters"] = ["Mara"]
    plan["scenes"][1]["dialogue"] = [{"character": "Mara", "line": "Who is there?"}]
    report = validate(plan)
    assert report.ok, report.errors


def test_an_unknown_animation_method_fails() -> None:
    plan = good_plan(6)
    plan["scenes"][0]["preferred_method"] = "deepfake"
    report = validate(plan)
    assert any("not one of" in e for e in report.errors)


def test_lipsync_cannot_be_requested_by_the_writer() -> None:
    """Lip-sync is derived from dialogue and a visible speaker, not asked for."""
    plan = good_plan(6)
    plan["scenes"][0]["preferred_method"] = "lipsync"
    report = validate(plan)
    assert any("not one of" in e for e in report.errors)


def test_a_plan_far_from_the_target_length_fails() -> None:
    plan = good_plan(6)
    for scene in plan["scenes"]:
        scene["target_duration_s"] = 3.0  # 18s against a 60s target
    report = validate(plan)
    assert any("more than" in e and "out" in e for e in report.errors)


def test_a_plan_slightly_off_target_only_warns() -> None:
    plan = good_plan(6)
    for scene in plan["scenes"]:
        scene["target_duration_s"] = 8.0  # 48s against 60s, 20% out
    report = validate(plan)
    assert report.ok, report.errors
    assert any("add up to" in w for w in report.warnings)


def test_overlong_narration_for_a_short_shot_warns_without_blocking() -> None:
    plan = good_plan(6)
    plan["scenes"][0]["narration"] = " ".join(["word"] * 80)
    report = validate(plan)
    assert report.ok, report.errors
    assert any("will have to run longer" in w for w in report.warnings)


def test_motion_over_budget_warns_rather_than_blocking() -> None:
    """The router downgrades shots; the plan itself is still usable."""
    plan = good_plan(6)
    for scene in plan["scenes"]:
        scene["preferred_method"] = "remote_i2v"
    report = validate(plan, motion_budget_s=10.0)
    assert report.ok, report.errors
    assert report.generated_motion_requested_s == 60.0
    assert any("downgrade" in w for w in report.warnings)


def test_a_camera_move_is_not_counted_as_requested_motion() -> None:
    report = validate(good_plan(6))
    assert report.generated_motion_requested_s == 0.0


def test_an_unknown_image_hint_only_warns() -> None:
    plan = good_plan(6)
    plan["scenes"][0]["image_hint"] = "absent.jpg"
    report = validate(
        plan, known_images=(ImageHint(image_id=1, filename="present.jpg"),)
    )
    assert report.ok, report.errors
    assert any("not in the library" in w for w in report.warnings)


def test_word_count_covers_narration_and_dialogue() -> None:
    plan = good_plan(2)
    for scene in plan["scenes"]:
        scene["target_duration_s"] = 30.0
    plan["scenes"][0]["narration"] = "One two three"
    plan["scenes"][0]["characters"] = ["Mara"]
    plan["scenes"][0]["dialogue"] = [{"character": "Mara", "line": "four five"}]
    plan["scenes"][1]["narration"] = "six"
    report = validate(plan)
    assert report.word_count == 6


def test_a_reply_that_is_not_an_object_fails() -> None:
    report = validate(["not", "a", "plan"])  # type: ignore[arg-type]
    assert any("not a JSON object" in e for e in report.errors)


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


def test_the_prompt_states_the_motion_budget() -> None:
    prompt = scene_plan.build_prompt(request_for(), motion_budget_s=30.0)
    assert "30 seconds" in prompt
    assert "camera_move" in prompt


def test_a_zero_budget_tells_the_writer_there_is_no_motion() -> None:
    prompt = scene_plan.build_prompt(request_for(), motion_budget_s=0.0)
    assert "No generated motion is available" in prompt


def test_the_prompt_lists_only_the_supplied_filenames() -> None:
    prompt = scene_plan.build_prompt(
        request_for(available_images=(ImageHint(image_id=1, filename="mara-01.png"),)),
        motion_budget_s=10.0,
    )
    assert "mara-01.png" in prompt


def test_previous_problems_are_fed_back_into_the_prompt() -> None:
    prompt = scene_plan.build_prompt(
        request_for(previous_problems=("Scene 3 repeats the narration of scene 1.",)),
        motion_budget_s=10.0,
    )
    assert "Fix all of them" in prompt
    assert "Scene 3 repeats" in prompt


# ---------------------------------------------------------------------------
# Gemini adapter, entirely mocked
# ---------------------------------------------------------------------------


def test_gemini_is_unconfigured_without_a_key() -> None:
    assert not GeminiScriptProvider({}).is_configured
    assert GeminiScriptProvider({"api_key": "k"}).is_configured


def test_gemini_declares_its_free_tier_without_inventing_a_number() -> None:
    caps = GeminiScriptProvider({}).capabilities()
    assert caps.endpoint_verified is True
    # The daily allowance is volatile and project-specific; claiming one
    # would be a number the user would trust and act on.
    assert "does not display a number it cannot verify" in caps.free_tier_note


async def test_gemini_refuses_to_call_without_a_key() -> None:
    with pytest.raises(ScriptProviderError) as caught:
        await GeminiScriptProvider({}).write(request_for())
    assert caught.value.error_class == ErrorClass.AUTH


async def test_gemini_rejects_a_model_it_does_not_declare() -> None:
    with pytest.raises(ScriptProviderError) as caught:
        await GeminiScriptProvider({"api_key": "k"}).write(
            request_for(), model_id="gemini-made-up"
        )
    assert caught.value.error_class == ErrorClass.INVALID_INPUT


@respx.mock
async def test_gemini_sends_the_documented_payload_shape() -> None:
    route = respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(200, json=gemini_response(good_plan()))
    )
    provider = GeminiScriptProvider({"api_key": "secret-key"})
    await provider.write(request_for())
    await provider.aclose()

    body = json.loads(route.calls.last.request.content)
    assert body["contents"][0]["parts"][0]["text"]
    text_format = body["generationConfig"]["responseFormat"]["text"]
    assert text_format["mimeType"] == "APPLICATION_JSON"
    assert text_format["schema"]["required"] == ["title", "logline", "scenes"]
    assert route.calls.last.request.headers["x-goog-api-key"] == "secret-key"


@respx.mock
async def test_gemini_parses_a_plan() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(200, json=gemini_response(good_plan(4)))
    )
    provider = GeminiScriptProvider({"api_key": "k"})
    result = await provider.write(request_for())
    await provider.aclose()

    assert result.title == "The Kettle"
    assert len(result.scenes) == 4
    assert result.usage["promptTokenCount"] == 900


@respx.mock
async def test_a_rate_limit_is_classified_and_not_retried_blindly() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"message": "quota exceeded"}}, headers={"retry-after": "47"}
        )
    )
    provider = GeminiScriptProvider({"api_key": "k"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()

    assert caught.value.error_class == ErrorClass.RATE_LIMITED
    assert caught.value.retry_after_s == 47.0


@respx.mock
async def test_a_rejected_key_is_an_auth_error() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "API key not valid"}})
    )
    provider = GeminiScriptProvider({"api_key": "wrong"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()
    assert caught.value.error_class == ErrorClass.AUTH


@respx.mock
async def test_an_error_body_never_leaks_the_key() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            400, text="Bad key: Bearer sk-abcdef1234567890 was rejected"
        )
    )
    provider = GeminiScriptProvider({"api_key": "sk-abcdef1234567890"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()
    assert "sk-abcdef1234567890" not in str(caught.value)


@respx.mock
async def test_a_blocked_prompt_is_a_moderation_error() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    provider = GeminiScriptProvider({"api_key": "k"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()
    assert caught.value.error_class == ErrorClass.MODERATION


@respx.mock
async def test_a_truncated_reply_says_so_instead_of_blaming_the_json() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"title": "half'}]}, "finishReason": "MAX_TOKENS"}
                ]
            },
        )
    )
    provider = GeminiScriptProvider({"api_key": "k"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()
    assert "output limit" in str(caught.value)


@respx.mock
async def test_a_timeout_is_transient_because_nothing_is_charged() -> None:
    respx.post(GENERATE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    provider = GeminiScriptProvider({"api_key": "k"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()
    assert caught.value.error_class == ErrorClass.TRANSIENT


@respx.mock
async def test_non_json_text_despite_a_schema_is_transient() -> None:
    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Sorry, I cannot."}]}}]},
        )
    )
    provider = GeminiScriptProvider({"api_key": "k"})
    with pytest.raises(ScriptProviderError) as caught:
        await provider.write(request_for())
    await provider.aclose()
    assert caught.value.error_class == ErrorClass.TRANSIENT


def test_the_declared_default_model_is_one_the_adapter_accepts() -> None:
    caps = GeminiScriptProvider({}).capabilities()
    assert caps.default_model in caps.models
    assert gemini_module.DEFAULT_MODEL in gemini_module.MODELS


# ---------------------------------------------------------------------------
# Manual paste
# ---------------------------------------------------------------------------


def test_paste_accepts_plain_json() -> None:
    result = ManualPasteScriptProvider().parse(json.dumps(good_plan(3)))
    assert result.title == "The Kettle"
    assert len(result.scenes) == 3


def test_paste_strips_a_markdown_fence() -> None:
    fenced = "```json\n" + json.dumps(good_plan(2)) + "\n```"
    assert len(ManualPasteScriptProvider().parse(fenced).scenes) == 2


def test_paste_recovers_json_after_a_chatty_preamble() -> None:
    chatty = "Sure! Here is the plan:\n" + json.dumps(good_plan(2))
    assert len(ManualPasteScriptProvider().parse(chatty).scenes) == 2


def test_an_empty_paste_is_refused() -> None:
    with pytest.raises(ScriptProviderError, match="Nothing was pasted"):
        ManualPasteScriptProvider().parse("   ")


def test_a_paste_with_no_json_is_refused() -> None:
    with pytest.raises(ScriptProviderError, match="does not contain a JSON object"):
        ManualPasteScriptProvider().parse("I would rather not.")


def test_broken_json_reports_the_parse_error() -> None:
    with pytest.raises(ScriptProviderError, match="not valid JSON"):
        ManualPasteScriptProvider().parse('{"title": "x", "scenes": [}')


async def test_the_paste_provider_never_calls_anything() -> None:
    with pytest.raises(ScriptProviderError, match="does not call anything"):
        await ManualPasteScriptProvider().write(request_for())


def test_the_paste_provider_is_not_advertised_as_automated() -> None:
    caps = ManualPasteScriptProvider().capabilities()
    assert caps.automated is False
    assert ManualPasteScriptProvider().is_configured is True


# ---------------------------------------------------------------------------
# The service: storing, approval and stage bookkeeping
# ---------------------------------------------------------------------------


@respx.mock
async def test_generating_stores_scenes_and_completes_the_stage(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVM_GEMINI_API_KEY", "test-key")
    from app import config
    from app.providers import script as registry

    config.get_settings.cache_clear()
    registry.reset_cache()

    respx.post(GENERATE_URL).mock(
        return_value=httpx.Response(200, json=gemini_response(good_plan(6)))
    )
    project_id = await make_project(session)

    script = await script_service.generate(session, project_id, provider_key="gemini")
    await session.commit()

    assert script.is_valid
    assert script.version == 1
    scenes = (
        await session.execute(select(Scene).where(Scene.script_id == script.id))
    ).scalars().all()
    assert len(scenes) == 6

    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SCRIPT
    )
    assert checkpoint.status == StageStatus.COMPLETED
    await registry.close_all()
    registry.reset_cache()


async def test_an_invalid_plan_is_still_stored_with_its_problems(
    session: AsyncSession,
) -> None:
    """Throwing it away would lose what the retry needs to do better."""
    project_id = await make_project(session)
    plan = good_plan(6)
    plan["scenes"][2]["narration"] = None
    plan["scenes"][2]["dialogue"] = []

    script = await script_service.accept_paste(session, project_id, json.dumps(plan))
    await session.commit()

    assert script.is_valid is False
    assert script.validation["errors"]
    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SCRIPT
    )
    assert checkpoint.status == StageStatus.FAILED


async def test_an_invalid_script_cannot_be_approved(session: AsyncSession) -> None:
    project_id = await make_project(session)
    plan = good_plan(6)
    plan["scenes"][0]["preferred_method"] = "nonsense"
    script = await script_service.accept_paste(session, project_id, json.dumps(plan))
    await session.commit()

    with pytest.raises(ScriptError, match="cannot be approved"):
        await script_service.approve(session, script.id)


async def test_a_valid_script_can_be_approved_and_withdrawn(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    script = await script_service.accept_paste(session, project_id, json.dumps(good_plan(6)))
    await session.commit()

    approved = await script_service.approve(session, script.id)
    assert approved.approved is True
    assert (await script_service.approved_script(session, project_id)).id == script.id

    await script_service.unapprove(session, script.id)
    assert await script_service.approved_script(session, project_id) is None


async def test_nothing_is_approved_by_default(session: AsyncSession) -> None:
    project_id = await make_project(session)
    await script_service.accept_paste(session, project_id, json.dumps(good_plan(6)))
    await session.commit()
    assert await script_service.approved_script(session, project_id) is None


async def test_versions_increment_and_the_newest_is_current(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    await script_service.accept_paste(session, project_id, json.dumps(good_plan(6)))
    second = await script_service.accept_paste(
        session, project_id, json.dumps(good_plan(6, title="Second"))
    )
    await session.commit()

    assert second.version == 2
    current = await project_service.current_script(session, project_id)
    assert current.title == "Second"


async def test_characters_named_in_the_plan_become_rows(session: AsyncSession) -> None:
    project_id = await make_project(session)
    plan = good_plan(6)
    plan["scenes"][0]["characters"] = ["Mara", "Idris"]
    plan["scenes"][1]["characters"] = ["Mara"]

    await script_service.accept_paste(session, project_id, json.dumps(plan))
    await session.commit()

    names = sorted(
        (
            await session.execute(
                select(Character.name).where(Character.project_id == project_id)
            )
        ).scalars()
    )
    assert names == ["Idris", "Mara"]


async def test_a_character_is_not_duplicated_across_versions(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    plan = good_plan(6)
    plan["scenes"][0]["characters"] = ["Mara"]

    await script_service.accept_paste(session, project_id, json.dumps(plan))
    await script_service.accept_paste(session, project_id, json.dumps(plan))
    await session.commit()

    names = (
        await session.execute(
            select(Character.name).where(Character.project_id == project_id)
        )
    ).scalars().all()
    assert names == ["Mara"]


async def test_scenes_are_matched_to_real_stills_by_filename(
    session: AsyncSession, tmp_path
) -> None:
    project_id = await make_project(session)
    first = await add_image(session, tmp_path, "kitchen.jpg")
    await add_image(session, tmp_path, "hallway.jpg")
    await session.commit()

    plan = good_plan(6)
    plan["scenes"][0]["image_hint"] = "kitchen.jpg"
    script = await script_service.accept_paste(session, project_id, json.dumps(plan))
    await session.commit()

    scenes = (
        await session.execute(
            select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
        )
    ).scalars().all()
    assert scenes[0].image_id == first.id


async def test_scenes_without_a_hint_spread_across_available_stills(
    session: AsyncSession, tmp_path
) -> None:
    """One still reused for every shot is what makes a video look automated."""
    project_id = await make_project(session)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        await add_image(session, tmp_path, name)
    await session.commit()

    script = await script_service.accept_paste(session, project_id, json.dumps(good_plan(3)))
    await session.commit()

    scenes = (
        await session.execute(
            select(Scene).where(Scene.script_id == script.id).order_by(Scene.order_index)
        )
    ).scalars().all()
    assert len({s.image_id for s in scenes}) == 3


async def test_a_missing_image_is_never_offered_to_the_writer(
    session: AsyncSession, tmp_path
) -> None:
    project_id = await make_project(session)
    present = await add_image(session, tmp_path, "present.jpg")
    gone = await add_image(session, tmp_path, "gone.jpg")
    gone.status = "missing"
    await session.commit()

    project = await project_service.get_project(session, project_id)
    offered = await script_service.available_images(session, project)
    assert [i.id for i in offered] == [present.id]


async def test_the_retry_prompt_carries_the_previous_problems(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    plan = good_plan(6)
    plan["scenes"][2]["narration"] = None
    plan["scenes"][2]["dialogue"] = []
    await script_service.accept_paste(session, project_id, json.dumps(plan))
    await session.commit()

    project = await project_service.get_project(session, project_id)
    problems = await script_service._recent_problems(session, project_id)
    assert any("neither narration nor dialogue" in p for p in problems)

    request = await script_service.build_request(
        session, project, previous_problems=problems
    )
    prompt = scene_plan.build_prompt(request, motion_budget_s=30.0)
    assert "Fix all of them" in prompt


async def test_generating_without_a_key_blocks_the_stage_rather_than_failing_it(
    session: AsyncSession,
) -> None:
    """A missing credential is the user's to fix, so it is not a failure."""
    from app.providers import script as registry

    registry.reset_cache()
    project_id = await make_project(session)

    with pytest.raises(ScriptError, match="no credentials"):
        await script_service.generate(session, project_id, provider_key="gemini")
    await session.commit()

    checkpoint = await project_service.get_checkpoint(
        session, project_id, PipelineStage.SCRIPT
    )
    assert checkpoint.status == StageStatus.BLOCKED
    assert "Add a key" in (checkpoint.blocked_reason or "")


async def test_the_paste_provider_cannot_be_used_for_generation(
    session: AsyncSession,
) -> None:
    project_id = await make_project(session)
    with pytest.raises(ScriptError, match="does not generate anything"):
        await script_service.generate(session, project_id, provider_key="manual_paste")


async def test_the_default_writer_falls_back_to_paste_without_a_key() -> None:
    from app.providers import script as registry

    registry.reset_cache()
    assert registry.default_key() == "manual_paste"


async def test_the_default_writer_is_gemini_once_a_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import config
    from app.providers import script as registry

    monkeypatch.setenv("AVM_GEMINI_API_KEY", "test-key")
    config.get_settings.cache_clear()
    registry.reset_cache()
    try:
        assert registry.default_key() == "gemini"
    finally:
        await registry.close_all()
        registry.reset_cache()


# ---------------------------------------------------------------------------
# The screens, rendered for real
#
# The service-level tests above never render a template, which is exactly
# how a lazy relationship load slips through: it only fails once Jinja
# touches the attribute outside the event loop.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An app instance without the lifespan, so no scheduler is started."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_the_projects_page_renders(client: AsyncClient) -> None:
    response = await client.get("/projects")
    assert response.status_code == 200
    assert "New project" in response.text


async def test_a_project_can_be_created_and_opened_over_http(
    client: AsyncClient,
) -> None:
    created = await client.post(
        "/projects",
        data={
            "name": "HTTP project",
            "target_duration_s": "60",
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "fps": "24",
        },
    )
    assert created.status_code == 200
    assert "HTTP project" in created.text

    detail = await client.get("/projects/1")
    assert detail.status_code == 200
    # Every pipeline stage is listed, all pending.
    assert "publish_assets" in detail.text


async def test_the_script_page_renders_with_the_prompt(client: AsyncClient) -> None:
    await client.post("/projects", data={"name": "P", "target_duration_s": "60"})
    response = await client.get("/projects/1/script")
    assert response.status_code == 200
    assert "You are writing an original short film" in response.text


async def test_pasting_a_plan_over_http_renders_the_scene_table(
    client: AsyncClient, session: AsyncSession, tmp_path
) -> None:
    """Regression: the scene table shows each still, which lazy-loaded."""
    await add_image(session, tmp_path, "kitchen.jpg")
    await session.commit()
    await client.post("/projects", data={"name": "P", "target_duration_s": "60"})

    plan = good_plan(6)
    plan["scenes"][0]["image_hint"] = "kitchen.jpg"
    response = await client.post(
        "/projects/1/script/paste", data={"pasted": json.dumps(plan)}
    )

    assert response.status_code == 200, response.text[:400]
    assert "kitchen.jpg" in response.text
    assert "Approve this version" in response.text


async def test_approving_over_http_marks_the_script_approved(
    client: AsyncClient, session: AsyncSession
) -> None:
    await client.post("/projects", data={"name": "P", "target_duration_s": "60"})
    await client.post(
        "/projects/1/script/paste", data={"pasted": json.dumps(good_plan(6))}
    )

    response = await client.post("/scripts/1/approve")
    assert response.status_code == 200
    assert "approved" in response.headers.get("X-AVM-Flash", "").lower()

    stored = (await session.execute(select(Script).where(Script.id == 1))).scalar_one()
    await session.refresh(stored)
    assert stored.approved is True


async def test_an_invalid_paste_is_rejected_with_a_readable_reason(
    client: AsyncClient,
) -> None:
    await client.post("/projects", data={"name": "P", "target_duration_s": "60"})
    response = await client.post("/projects/1/script/paste", data={"pasted": "nonsense"})
    assert response.status_code == 400
    assert "JSON" in response.json()["detail"]


async def test_an_invalid_plan_renders_its_problems(client: AsyncClient) -> None:
    await client.post("/projects", data={"name": "P", "target_duration_s": "60"})
    plan = good_plan(6)
    plan["scenes"][2]["narration"] = None
    plan["scenes"][2]["dialogue"] = []

    response = await client.post(
        "/projects/1/script/paste", data={"pasted": json.dumps(plan)}
    )
    assert response.status_code == 200
    assert "cannot be approved" in response.text
    assert "neither narration nor dialogue" in response.text
    assert "Approve this version" not in response.text
