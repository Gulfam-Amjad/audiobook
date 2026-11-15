"""Provider adapter tests.

Every call is intercepted by respx, so these tests never reach a provider and
never spend a credit. They cover two things:

1. A contract every adapter must satisfy, checked by iterating the registry -
   so a twelfth adapter added later is held to the same standard without
   anyone remembering to write a test for it.
2. Per-adapter request and response handling against fixtures shaped like the
   documented payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.constants import ErrorClass, ProviderKind
from app.providers import registry
from app.providers.base import GenerationRequest, ProviderError
from app.providers.http_adapter import HttpProviderAdapter
from tests.conftest import write_image

pytestmark = pytest.mark.usefixtures("data_root")

#: Adapters that reach the network. The mock and manual adapters do not.
HTTP_KEYS = (
    "alibaba",
    "byteplus",
    "fal",
    "replicate",
    "minimax",
    "pixverse",
    "runway",
    "luma",
    "pika",
    "vidu",
    "kling",
)

FAKE_CREDENTIALS = {
    "api_key": "test-key",
    "api_token": "test-token",
    "access_key": "test-access",
    "secret_key": "test-secret",
    "region": "ap-southeast-1",
    "group_id": "test-group",
}


@pytest.fixture
def source_image(tmp_path: Path) -> Path:
    return write_image(tmp_path / "frame.jpg", size=(1280, 720))


def make_adapter(key: str) -> HttpProviderAdapter:
    """Build an adapter with fake credentials, bypassing the env."""
    cls = registry._load_class(key)  # noqa: SLF001 - tests need a clean instance
    assert cls is not None, f"adapter '{key}' failed to import"
    caps_fields = cls({}).capabilities().required_credentials
    return cls({name: FAKE_CREDENTIALS[name] for name in caps_fields})


def make_request(
    adapter, source_image: Path, *, model_id: str | None = None, duration_s: int = 5
) -> GenerationRequest:
    caps = adapter.capabilities()
    model = caps.model(model_id) if model_id else caps.default_model
    assert model is not None
    return GenerationRequest(
        job_id=1,
        client_token="avm-test-token",
        image_path=str(source_image),
        prompt="A slow push in on the subject.",
        negative_prompt=None,
        model_id=model.id,
        duration_s=duration_s if not model.durations else model.durations[0],
        resolution=model.resolutions[0] if model.resolutions else "720p",
        aspect_ratio=model.aspect_ratios[0] if model.aspect_ratios else "16:9",
    )


# ---------------------------------------------------------------------------
# The contract every adapter must meet
# ---------------------------------------------------------------------------


def test_every_registered_adapter_imports() -> None:
    caps = registry.all_capabilities()
    assert set(caps) == set(registry.ADAPTER_PATHS), registry.IMPORT_ERRORS
    assert not registry.IMPORT_ERRORS


@pytest.mark.parametrize("key", list(registry.ADAPTER_PATHS))
def test_capabilities_are_self_consistent(key: str) -> None:
    caps = registry.get_capabilities(key)
    assert caps is not None
    assert caps.key == key, "the capability key must match the registry key"
    assert caps.display_name
    assert caps.models, "an adapter with no model cannot be matched to a job"
    assert caps.checked_on, "every adapter must record when its facts were checked"
    assert len({m.id for m in caps.models}) == len(caps.models), "duplicate model ids"
    assert caps.default_concurrency >= 1
    assert caps.min_poll_interval_s >= 1


@pytest.mark.parametrize("key", list(registry.ADAPTER_PATHS))
def test_every_adapter_states_its_free_tier_and_automation_position(key: str) -> None:
    """These two sentences are the ones that stop a wrong assumption later."""
    caps = registry.get_capabilities(key)
    assert caps is not None
    assert caps.free_tier_note, f"{key} does not say what its free allowance is"
    assert caps.automation_note, f"{key} does not say what its terms allow"


@pytest.mark.parametrize("key", HTTP_KEYS)
def test_priced_models_cite_a_source(key: str) -> None:
    """A price with no URL and date cannot be audited, so it is not allowed."""
    caps = registry.get_capabilities(key)
    assert caps is not None
    for model in caps.models:
        price = model.price
        if price.mode == "unknown" and not price.usd_per_second and not price.usd_per_video:
            # Declaring "we do not know" is fine; claiming a number is not.
            assert price.confidence == "unknown", f"{key}/{model.id}"
            continue
        assert price.source_url, f"{key}/{model.id} quotes a price with no source"
        assert price.checked_on, f"{key}/{model.id} quotes a price with no date"
        assert price.confidence in ("verified", "estimated"), f"{key}/{model.id}"


@pytest.mark.parametrize("key", HTTP_KEYS)
def test_unverified_endpoints_say_so_in_their_notes(key: str) -> None:
    caps = registry.get_capabilities(key)
    assert caps is not None
    if caps.endpoint_verified:
        return
    joined = " ".join(caps.integration_notes).lower()
    assert "verify" in joined or "not confirmed" in joined, (
        f"{key} has an unverified endpoint but its notes do not warn about it"
    )


@pytest.mark.parametrize("key", list(registry.ADAPTER_PATHS))
def test_configured_means_every_required_credential_is_present(key: str) -> None:
    """An adapter must not report itself ready while a credential is missing.

    The mock is the one adapter that supplies its own stand-in key, which is
    why this checks the invariant rather than asserting an empty instance is
    always unconfigured.
    """
    cls = registry._load_class(key)  # noqa: SLF001
    assert cls is not None
    adapter = cls({})
    required = adapter.capabilities().required_credentials
    expected = all(adapter.credentials.get(name) for name in required)
    assert adapter.is_configured is expected

    if key not in ("mock", "manual"):
        assert not adapter.is_configured, f"{key} claims to be ready with no credentials"


@pytest.mark.parametrize("key", HTTP_KEYS)
def test_credentials_never_appear_in_the_capability_description(key: str) -> None:
    """Capabilities are rendered into HTML, so they must stay secret-free."""
    adapter = make_adapter(key)
    rendered = repr(adapter.capabilities())
    for value in FAKE_CREDENTIALS.values():
        assert value not in rendered


def prime_uploads(router: respx.MockRouter, key: str) -> None:
    """Let a two-step adapter get past its upload so the generate call runs.

    Pika and PixVerse upload the still before asking for a video. Those tests
    that care about the generation request have to satisfy the upload first.
    """
    if key == "pixverse":
        router.post("https://app-api.pixverse.ai/openapi/v2/image/upload").mock(
            return_value=httpx.Response(200, json={"ErrCode": 0, "Resp": {"img_id": 1}})
        )
    elif key == "pika":
        router.post("https://api.dev.pika.art/v1/media/uploads").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.invalid/put/x",
                    "public_url": "https://media.invalid/x.jpg",
                },
            )
        )
        router.put("https://storage.invalid/put/x").mock(return_value=httpx.Response(200))


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_a_submit_timeout_is_reported_as_uncertain(
    key: str, source_image: Path
) -> None:
    """The one failure mode that must never be retried automatically.

    If the request that creates the video times out, the provider may have
    accepted and charged for it. Calling that merely transient would let the
    scheduler pay twice.
    """
    adapter = make_adapter(key)
    request = make_request(adapter, source_image)

    with respx.mock(assert_all_called=False) as router:
        prime_uploads(router, key)
        router.route().mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.UNCERTAIN
    await adapter.aclose()


@pytest.mark.parametrize("key", ("pixverse", "pika"))
async def test_a_timeout_while_uploading_is_only_transient(
    key: str, source_image: Path
) -> None:
    """Nothing has been generated yet, so this one is safe to retry."""
    adapter = make_adapter(key)
    request = make_request(adapter, source_image)

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.TRANSIENT
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_an_unauthorised_response_is_classified_as_auth(
    key: str, source_image: Path
) -> None:
    adapter = make_adapter(key)
    request = make_request(adapter, source_image)

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(401, json={"error": "bad key"}))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.AUTH
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_a_rate_limited_response_carries_retry_after(
    key: str, source_image: Path
) -> None:
    adapter = make_adapter(key)
    request = make_request(adapter, source_image)

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
        )
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.RATE_LIMITED
    assert caught.value.retry_after_s == 30.0
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_a_payment_required_response_is_not_retryable(
    key: str, source_image: Path
) -> None:
    adapter = make_adapter(key)
    request = make_request(adapter, source_image)

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(402, json={"error": "no credit"}))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.INSUFFICIENT_CREDIT
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_an_oversized_image_is_refused_before_any_request(
    key: str, tmp_path: Path
) -> None:
    """A file the provider would reject must not cost a round trip."""
    adapter = make_adapter(key)
    caps = adapter.capabilities()
    if caps.image.max_bytes is None:
        pytest.skip(f"{key} declares no image size limit")

    huge = tmp_path / "huge.jpg"
    huge.write_bytes(b"\xff\xd8\xff" + b"\x00" * (caps.image.max_bytes + 1024))
    request = make_request(adapter, huge)

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.INVALID_INPUT
    assert not route.called, "the request went out despite the image being too big"
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_a_missing_source_file_is_an_input_error(key: str, tmp_path: Path) -> None:
    adapter = make_adapter(key)
    request = make_request(adapter, tmp_path / "does-not-exist.jpg")

    with respx.mock(assert_all_called=False) as router:
        router.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.INVALID_INPUT
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
def test_cost_estimates_are_either_sourced_or_admitted_unknown(key: str) -> None:
    adapter = make_adapter(key)
    caps = adapter.capabilities()
    for model in caps.models:
        request = make_request(adapter, Path("unused.jpg"), model_id=model.id)
        estimate = adapter.estimate_cost(request)
        if estimate.confidence == "unknown":
            assert estimate.usd == 0.0
            assert "no published price" in estimate.basis.lower() or estimate.basis
        else:
            assert estimate.usd > 0, f"{key}/{model.id} claims a confident price of $0"
            assert estimate.source_url, f"{key}/{model.id} priced without a source"
            assert estimate.basis


# ---------------------------------------------------------------------------
# Per-adapter request and response handling
# ---------------------------------------------------------------------------


async def test_runway_submits_and_polls(source_image: Path) -> None:
    adapter = make_adapter("runway")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://api.dev.runwayml.com") as router:
        submit = router.post("/v1/image_to_video").mock(
            return_value=httpx.Response(200, json={"id": "task-1"})
        )
        router.get("/v1/tasks/task-1").mock(
            return_value=httpx.Response(
                200, json={"status": "SUCCEEDED", "output": ["https://cdn.invalid/a.mp4"]}
            )
        )

        result = await adapter.submit(request)
        assert result.task_ref == "task-1"
        status = await adapter.poll("task-1")

    sent = submit.calls[0].request
    assert sent.headers["x-runway-version"] == "2024-11-06"
    assert sent.headers["authorization"] == "Bearer test-key"
    assert status.state == "succeeded"
    assert status.video_url == "https://cdn.invalid/a.mp4"
    await adapter.aclose()


async def test_runway_maps_aspect_ratio_to_an_output_size(source_image: Path) -> None:
    adapter = make_adapter("runway")
    request = make_request(adapter, source_image)
    request.aspect_ratio = "9:16"

    with respx.mock(base_url="https://api.dev.runwayml.com") as router:
        submit = router.post("/v1/image_to_video").mock(
            return_value=httpx.Response(200, json={"id": "task-2"})
        )
        await adapter.submit(request)

    assert submit.calls[0].request.read().find(b'"720:1280"') != -1
    await adapter.aclose()


async def test_runway_rejects_an_aspect_ratio_it_has_no_size_for(
    source_image: Path,
) -> None:
    adapter = make_adapter("runway")
    request = make_request(adapter, source_image)
    request.aspect_ratio = "5:2"

    with pytest.raises(ProviderError) as caught:
        await adapter.submit(request)
    assert caught.value.error_class == ErrorClass.INVALID_INPUT
    await adapter.aclose()


async def test_runway_throttled_counts_as_pending_not_failed(source_image: Path) -> None:
    adapter = make_adapter("runway")
    with respx.mock(base_url="https://api.dev.runwayml.com") as router:
        router.get("/v1/tasks/t").mock(
            return_value=httpx.Response(200, json={"status": "THROTTLED"})
        )
        status = await adapter.poll("t")
    assert status.state == "pending"
    await adapter.aclose()


async def test_luma_uses_a_keyframe_for_ten_second_clips(source_image: Path) -> None:
    adapter = make_adapter("luma")
    request = make_request(adapter, source_image, duration_s=10)
    request.duration_s = 10

    with respx.mock(base_url="https://agents.lumalabs.ai") as router:
        submit = router.post("/v1/generations").mock(
            return_value=httpx.Response(201, json={"id": "gen-1", "state": "queued"})
        )
        result = await adapter.submit(request)

    body = submit.calls[0].request.read()
    assert b'"keyframes"' in body
    assert b'"start_frame"' not in body
    assert result.task_ref == "gen-1"
    await adapter.aclose()


async def test_luma_uses_a_start_frame_for_five_second_clips(source_image: Path) -> None:
    adapter = make_adapter("luma")
    request = make_request(adapter, source_image)
    request.duration_s = 5

    with respx.mock(base_url="https://agents.lumalabs.ai") as router:
        submit = router.post("/v1/generations").mock(
            return_value=httpx.Response(201, json={"id": "gen-2"})
        )
        await adapter.submit(request)

    assert b'"start_frame"' in submit.calls[0].request.read()
    await adapter.aclose()


def test_luma_prices_ten_seconds_at_three_times_five_seconds(
    source_image: Path,
) -> None:
    """Flattening this to a per-second rate would misquote one of the two."""
    adapter = make_adapter("luma")
    five = make_request(adapter, source_image)
    five.duration_s, five.resolution = 5, "720p"
    ten = make_request(adapter, source_image)
    ten.duration_s, ten.resolution = 10, "720p"

    assert adapter.estimate_cost(five).usd == pytest.approx(0.30)
    assert adapter.estimate_cost(ten).usd == pytest.approx(0.90)


async def test_vidu_sends_a_token_auth_header(source_image: Path) -> None:
    adapter = make_adapter("vidu")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://api.vidu.com") as router:
        submit = router.post("/ent/v2/img2video").mock(
            return_value=httpx.Response(200, json={"task_id": "v-1"})
        )
        await adapter.submit(request)

    assert submit.calls[0].request.headers["authorization"] == "Token test-key"
    await adapter.aclose()


async def test_vidu_reads_the_url_out_of_creations(source_image: Path) -> None:
    adapter = make_adapter("vidu")
    with respx.mock(base_url="https://api.vidu.com") as router:
        router.get("/ent/v2/tasks/v-1/creations").mock(
            return_value=httpx.Response(
                200,
                json={"state": "success", "creations": [{"url": "https://cdn.invalid/v.mp4"}]},
            )
        )
        status = await adapter.poll("v-1")
    assert status.state == "succeeded"
    assert status.video_url == "https://cdn.invalid/v.mp4"
    await adapter.aclose()


def test_vidu_marks_its_dollar_prices_as_estimates() -> None:
    """Vidu publishes credits and RMB only, so USD here is a conversion."""
    caps = registry.get_capabilities("vidu")
    assert caps is not None
    for model in caps.models:
        assert model.price.confidence == "estimated"
        assert "assumed" in model.price.notes.lower()


async def test_minimax_detects_a_failure_hidden_in_a_200(source_image: Path) -> None:
    """MiniMax reports refusals inside an HTTP 200 body."""
    adapter = make_adapter("minimax")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://api.minimax.io") as router:
        router.post("/v2/video_generation").mock(
            return_value=httpx.Response(
                200,
                json={"base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}},
            )
        )
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    assert caught.value.error_class == ErrorClass.INSUFFICIENT_CREDIT
    await adapter.aclose()


async def test_minimax_resolves_a_file_id_into_a_download_url(source_image: Path) -> None:
    adapter = make_adapter("minimax")
    with respx.mock(base_url="https://api.minimax.io") as router:
        router.get("/v2/query/video_generation/t-1").mock(
            return_value=httpx.Response(200, json={"status": "Success", "file_id": "f-9"})
        )
        retrieve = router.get("/v1/files/retrieve").mock(
            return_value=httpx.Response(
                200, json={"file": {"download_url": "https://cdn.invalid/m.mp4"}}
            )
        )
        status = await adapter.poll("t-1")

    assert retrieve.calls[0].request.url.params["file_id"] == "f-9"
    assert status.video_url == "https://cdn.invalid/m.mp4"
    await adapter.aclose()


async def test_minimax_uses_the_legacy_endpoint_for_hailuo_models(
    source_image: Path,
) -> None:
    adapter = make_adapter("minimax")
    request = make_request(adapter, source_image, model_id="MiniMax-Hailuo-2.3")

    with respx.mock(base_url="https://api.minimax.io") as router:
        legacy = router.post("/v1/video_generation").mock(
            return_value=httpx.Response(200, json={"task_id": "h-1"})
        )
        await adapter.submit(request)

    assert b'"first_frame_image"' in legacy.calls[0].request.read()
    await adapter.aclose()


async def test_pixverse_uploads_the_image_then_generates(source_image: Path) -> None:
    adapter = make_adapter("pixverse")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://app-api.pixverse.ai") as router:
        upload = router.post("/openapi/v2/image/upload").mock(
            return_value=httpx.Response(200, json={"ErrCode": 0, "Resp": {"img_id": 77}})
        )
        generate = router.post("/openapi/v2/video/img/generate").mock(
            return_value=httpx.Response(200, json={"ErrCode": 0, "Resp": {"video_id": 12345}})
        )
        result = await adapter.submit(request)

    assert upload.called
    body = json.loads(generate.calls[0].request.read())
    assert body["img_id"] == 77, "the generate call must reference the uploaded image"
    assert generate.calls[0].request.headers.get("ai-trace-id")
    assert result.task_ref == "12345"
    await adapter.aclose()


async def test_pixverse_treats_a_moderation_state_as_final(source_image: Path) -> None:
    adapter = make_adapter("pixverse")
    with respx.mock(base_url="https://app-api.pixverse.ai") as router:
        router.get("/openapi/v2/video/result/5").mock(
            return_value=httpx.Response(200, json={"ErrCode": 0, "Resp": {"status": 7}})
        )
        status = await adapter.poll("5")
    assert status.state == "failed"
    assert status.error_class == ErrorClass.MODERATION
    assert "refunded" in status.message
    await adapter.aclose()


async def test_pixverse_sends_a_unique_trace_id_per_request(source_image: Path) -> None:
    adapter = make_adapter("pixverse")
    with respx.mock(base_url="https://app-api.pixverse.ai") as router:
        route = router.get("/openapi/v2/video/result/5").mock(
            return_value=httpx.Response(200, json={"ErrCode": 0, "Resp": {"status": 5}})
        )
        await adapter.poll("5")
        await adapter.poll("5")

    first = route.calls[0].request.headers["ai-trace-id"]
    second = route.calls[1].request.headers["ai-trace-id"]
    assert first != second
    await adapter.aclose()


async def test_kling_signs_a_jwt_rather_than_sending_the_secret(
    source_image: Path,
) -> None:
    adapter = make_adapter("kling")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://api-singapore.klingai.com") as router:
        submit = router.post(f"/image-to-video/{request.model_id}").mock(
            return_value=httpx.Response(200, json={"data": {"task_id": "k-1"}})
        )
        result = await adapter.submit(request)

    header = submit.calls[0].request.headers["authorization"]
    assert header.startswith("Bearer eyJ"), "expected a JWT, not a raw key"
    assert "test-secret" not in header, "the secret key must never be transmitted"
    assert result.task_ref == "k-1"
    await adapter.aclose()


async def test_kling_reads_the_first_task_from_a_list(source_image: Path) -> None:
    adapter = make_adapter("kling")
    with respx.mock(base_url="https://api-singapore.klingai.com") as router:
        router.get("/tasks").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "task_status": "succeed",
                            "task_result": {"videos": [{"url": "https://cdn.invalid/k.mp4"}]},
                        }
                    ]
                },
            )
        )
        status = await adapter.poll("k-1")
    assert status.video_url == "https://cdn.invalid/k.mp4"
    await adapter.aclose()


async def test_fal_keeps_the_model_path_in_its_task_reference(
    source_image: Path,
) -> None:
    """fal's status URL is model-scoped, so polling needs both halves."""
    adapter = make_adapter("fal")
    request = make_request(adapter, source_image, model_id="fal-ai/wan/v2.2-a14b/image-to-video")

    with respx.mock(base_url="https://queue.fal.run") as router:
        router.post("/fal-ai/wan/v2.2-a14b/image-to-video").mock(
            return_value=httpx.Response(200, json={"request_id": "r-1"})
        )
        result = await adapter.submit(request)

    assert result.task_ref == "fal-ai/wan/v2.2-a14b/image-to-video|r-1"
    await adapter.aclose()


async def test_fal_fetches_the_result_only_once_completed(source_image: Path) -> None:
    adapter = make_adapter("fal")
    task_ref = "fal-ai/wan/v2.2-a14b/image-to-video|r-1"

    with respx.mock(base_url="https://queue.fal.run") as router:
        router.get("/fal-ai/wan/requests/r-1/status").mock(
            return_value=httpx.Response(200, json={"status": "IN_PROGRESS"})
        )
        result_route = router.get("/fal-ai/wan/requests/r-1").mock(
            return_value=httpx.Response(200, json={"video": {"url": "https://cdn.invalid/f.mp4"}})
        )
        running = await adapter.poll(task_ref)
        assert running.state == "running"
        assert not result_route.called

        router.get("/fal-ai/wan/requests/r-1/status").mock(
            return_value=httpx.Response(200, json={"status": "COMPLETED"})
        )
        done = await adapter.poll(task_ref)

    assert done.state == "succeeded"
    assert done.video_url == "https://cdn.invalid/f.mp4"
    await adapter.aclose()


async def test_replicate_converts_duration_into_frames(source_image: Path) -> None:
    adapter = make_adapter("replicate")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://api.replicate.com") as router:
        submit = router.post(
            "/v1/models/wan-video/wan-2.2-i2v-fast/predictions"
        ).mock(return_value=httpx.Response(201, json={"id": "p-1", "status": "starting"}))
        await adapter.submit(request)

    # 5 seconds at the model's fixed 16 fps, plus the leading frame.
    body = json.loads(submit.calls[0].request.read())
    assert body["input"]["num_frames"] == 81
    await adapter.aclose()


async def test_replicate_reports_the_cost_it_was_actually_charged(
    source_image: Path,
) -> None:
    adapter = make_adapter("replicate")
    with respx.mock(base_url="https://api.replicate.com") as router:
        router.get("/v1/predictions/p-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "output": "https://cdn.invalid/r.mp4",
                    "metrics": {"total_cost": 0.0732},
                },
            )
        )
        status = await adapter.poll("p-1")
    assert status.actual_cost_usd == pytest.approx(0.0732)
    await adapter.aclose()


@pytest.mark.parametrize("key", HTTP_KEYS)
def test_every_credential_offered_in_env_is_one_the_adapter_uses(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field in .env that nothing reads is a silent trap.

    Both ``AVM_ALIBABA_REGION`` and ``AVM_MINIMAX_GROUP_ID`` were offered in
    ``.env.example`` while the adapters ignored them, so a user could set one
    and wonder why nothing changed.
    """
    from app import config

    for name in (
        "ALIBABA_API_KEY", "ALIBABA_REGION", "BYTEPLUS_API_KEY", "FAL_API_KEY",
        "REPLICATE_API_TOKEN", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID",
        "PIXVERSE_API_KEY", "RUNWAY_API_KEY", "LUMA_API_KEY", "PIKA_API_KEY",
        "VIDU_API_KEY", "KLING_ACCESS_KEY", "KLING_SECRET_KEY",
    ):
        monkeypatch.setenv(f"AVM_{name}", "placeholder")
    config.get_settings.cache_clear()

    offered = set(config.get_settings().credential_for(key))
    caps = registry.get_capabilities(key)
    assert caps is not None
    declared = set(caps.required_credentials) | set(caps.optional_credentials)

    assert offered <= declared, (
        f"{key} is offered {sorted(offered - declared)} in .env but never declares it"
    )
    assert set(caps.required_credentials) <= offered, (
        f"{key} requires {sorted(set(caps.required_credentials) - offered)}, "
        "which .env has no field for"
    )
    config.get_settings.cache_clear()


def test_alibaba_defaults_to_the_region_its_free_quota_lives_in() -> None:
    from app.providers.alibaba import DEFAULT_REGION, REGION_HOSTS

    adapter = make_adapter("alibaba")
    assert adapter.base_url == REGION_HOSTS[DEFAULT_REGION]
    assert "intl" in adapter.base_url


def test_alibaba_honours_a_configured_region() -> None:
    """The two DashScope regions do not share accounts, keys or quota."""
    cls = registry._load_class("alibaba")  # noqa: SLF001
    assert cls is not None
    china = cls({"api_key": "k", "region": "beijing"})
    assert china.base_url == "https://dashscope.aliyuncs.com"


def test_an_unknown_alibaba_region_falls_back_rather_than_building_a_bad_url() -> None:
    cls = registry._load_class("alibaba")  # noqa: SLF001
    assert cls is not None
    adapter = cls({"api_key": "k", "region": "mars"})
    assert adapter.base_url == "https://dashscope-intl.aliyuncs.com"


async def test_minimax_passes_a_group_id_to_the_file_api_when_one_is_set() -> None:
    cls = registry._load_class("minimax")  # noqa: SLF001
    assert cls is not None
    adapter = cls({"api_key": "k", "group_id": "g-42"})

    with respx.mock(base_url="https://api.minimax.io") as router:
        router.get("/v2/query/video_generation/t").mock(
            return_value=httpx.Response(200, json={"status": "Success", "file_id": "f-1"})
        )
        retrieve = router.get("/v1/files/retrieve").mock(
            return_value=httpx.Response(200, json={"file": {"download_url": "https://x/y.mp4"}})
        )
        await adapter.poll("t")

    assert retrieve.calls[0].request.url.params["GroupId"] == "g-42"
    await adapter.aclose()


async def test_minimax_omits_the_group_id_when_none_is_configured(
    source_image: Path,
) -> None:
    """Newer keys carry their own group, so sending an empty one would 400."""
    adapter = make_adapter("minimax")
    with respx.mock(base_url="https://api.minimax.io") as router:
        router.get("/v2/query/video_generation/t").mock(
            return_value=httpx.Response(200, json={"status": "Success", "file_id": "f-1"})
        )
        retrieve = router.get("/v1/files/retrieve").mock(
            return_value=httpx.Response(200, json={"file": {"download_url": "https://x/y.mp4"}})
        )
        await adapter.poll("t")

    assert "GroupId" not in retrieve.calls[0].request.url.params
    await adapter.aclose()


async def test_alibaba_sends_the_async_header(source_image: Path) -> None:
    adapter = make_adapter("alibaba")
    request = make_request(adapter, source_image)

    with respx.mock(base_url="https://dashscope-intl.aliyuncs.com") as router:
        submit = router.post(
            "/api/v1/services/aigc/video-generation/video-synthesis"
        ).mock(return_value=httpx.Response(200, json={"output": {"task_id": "a-1"}}))
        await adapter.submit(request)

    assert submit.calls[0].request.headers["x-dashscope-async"] == "enable"
    await adapter.aclose()


async def test_alibaba_treats_an_expired_task_as_not_found(source_image: Path) -> None:
    """Task ids expire after 24 hours, which is not the same as a failure."""
    adapter = make_adapter("alibaba")
    with respx.mock(base_url="https://dashscope-intl.aliyuncs.com") as router:
        router.get("/api/v1/tasks/a-1").mock(
            return_value=httpx.Response(200, json={"output": {"task_status": "UNKNOWN"}})
        )
        status = await adapter.poll("a-1")
    assert status.state == "not_found"
    assert "24 hours" in status.message
    await adapter.aclose()


async def test_byteplus_reports_the_tokens_it_consumed(source_image: Path) -> None:
    adapter = make_adapter("byteplus")
    with respx.mock(base_url="https://ark.ap-southeast.bytepluses.com") as router:
        router.get("/api/v3/contents/generations/tasks/b-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "content": {"video_url": "https://cdn.invalid/b.mp4"},
                    "usage": {"total_tokens": 102960},
                },
            )
        )
        status = await adapter.poll("b-1")
    assert status.actual_units == pytest.approx(102960)
    await adapter.aclose()


def test_byteplus_price_is_derived_from_its_own_token_example() -> None:
    caps = registry.get_capabilities("byteplus")
    assert caps is not None
    fast = caps.model("seedance-1-0-pro-fast-251015")
    assert fast is not None
    # $1 per million tokens at 20,592 tokens/second works out to ~$0.02/s,
    # which is the $0.10 per 5-second clip BytePlus publishes.
    assert fast.price.usd_per_second["720p"] == pytest.approx(0.020592)
    assert fast.price.confidence == "estimated"


async def test_pika_uploads_through_a_presigned_put(source_image: Path) -> None:
    adapter = make_adapter("pika")
    request = make_request(adapter, source_image)

    with respx.mock(assert_all_called=False) as router:
        router.post("https://api.dev.pika.art/v1/media/uploads").mock(
            return_value=httpx.Response(
                200,
                json={
                    "upload_url": "https://storage.invalid/put/abc",
                    "public_url": "https://media.invalid/abc.jpg",
                },
            )
        )
        put = router.put("https://storage.invalid/put/abc").mock(
            return_value=httpx.Response(200)
        )
        submit = router.post(
            "https://api.dev.pika.art/v1/media/pika/pika-2.5/image-to-video"
        ).mock(return_value=httpx.Response(200, json={"id": "pk-1"}))

        result = await adapter.submit(request)

    assert put.called
    assert "authorization" not in put.calls[0].request.headers, (
        "the presigned PUT goes to object storage and must not carry the API key"
    )
    assert b"https://media.invalid/abc.jpg" in submit.calls[0].request.read()
    assert result.task_ref == "pk-1"
    await adapter.aclose()


async def test_pika_refuses_a_model_it_has_no_endpoint_for(source_image: Path) -> None:
    adapter = make_adapter("pika")
    request = make_request(adapter, source_image)
    request.model_id = "pika-9.9"

    with pytest.raises(ProviderError) as caught:
        await adapter.submit(request)
    assert caught.value.error_class == ErrorClass.INVALID_INPUT
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Error text that reaches the screen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        'Rejected header "Authorization: Bearer sk-abcdef1234567890"',
        '{"error": "invalid api_key: r8_abcdef1234567890"}',
        "access_key=AKIA0123456789 was not recognised",
    ],
)
def test_a_provider_error_body_cannot_leak_a_credential(body: str) -> None:
    """Providers do echo the offending header back, and we display that text."""
    from app.providers.http import sanitize_provider_message

    cleaned = sanitize_provider_message(body)
    for leak in ("sk-abcdef1234567890", "r8_abcdef1234567890", "AKIA0123456789"):
        assert leak not in cleaned
    assert "***" in cleaned


def test_sanitising_keeps_the_part_of_the_message_that_helps() -> None:
    from app.providers.http import sanitize_provider_message

    cleaned = sanitize_provider_message("Image\nresolution\ttoo low: 128x128 minimum 300")
    assert cleaned == "Image resolution too low: 128x128 minimum 300"


def test_a_very_long_error_body_is_truncated() -> None:
    from app.providers.http import sanitize_provider_message

    cleaned = sanitize_provider_message("x" * 5000)
    assert len(cleaned) == 400
    assert cleaned.endswith("\u2026")


@pytest.mark.parametrize("key", HTTP_KEYS)
async def test_an_error_message_names_the_provider_it_came_from(
    key: str, source_image: Path
) -> None:
    """Twelve adapters means "request failed" is useless without a name."""
    adapter = make_adapter(key)
    request = make_request(adapter, source_image)
    display_name = adapter.capabilities().display_name

    with respx.mock(assert_all_called=False) as router:
        prime_uploads(router, key)
        router.route().mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(ProviderError) as caught:
            await adapter.submit(request)

    message = caught.value.message
    assert display_name in message or key in message.lower(), message
    await adapter.aclose()


# ---------------------------------------------------------------------------
# Manual assist
# ---------------------------------------------------------------------------


def test_manual_assist_needs_no_credentials_and_costs_nothing() -> None:
    adapter = registry.get_adapter("manual")
    assert adapter is not None
    caps = adapter.capabilities()
    assert caps.kind == ProviderKind.MANUAL
    assert caps.required_credentials == ()
    assert adapter.is_configured
    assert all(rate == 0.0 for rate in caps.models[0].price.usd_per_second.values())


async def test_manual_assist_refuses_to_pretend_it_submitted_anything(
    source_image: Path,
) -> None:
    adapter = registry.get_adapter("manual")
    assert adapter is not None
    request = make_request(adapter, source_image)

    with pytest.raises(ProviderError) as caught:
        await adapter.submit(request)
    assert caught.value.error_class == ErrorClass.INVALID_INPUT
    assert "not submitted over the network" in caught.value.message


async def test_manual_assist_never_claims_a_balance() -> None:
    adapter = registry.get_adapter("manual")
    assert adapter is not None
    balance = await adapter.fetch_balance()
    assert balance.balance is None
    assert balance.source == "unknown"


def test_the_ten_manual_sites_are_described_honestly() -> None:
    from app.providers import manual_sites

    assert len(manual_sites.SITES) == 10
    for site in manual_sites.SITES:
        assert site.free_allowance, site.key
        assert site.allowance_source_url, site.key
        assert site.seconds_per_day_low <= site.seconds_per_day_high
        if site.automation_prohibited:
            assert site.automation_clause, f"{site.key} claims a ban with no quote"
            assert site.automation_source_url, site.key


def test_the_combined_free_allowance_falls_well_short_of_the_target() -> None:
    """If this ever stops being true, the capacity screen is lying."""
    from app.providers import manual_sites

    low, high = manual_sites.total_seconds_per_day()
    assert high < 20 * 60, (
        "the site data now claims the 20-minute daily target is reachable for free, "
        "which contradicts the research it came from"
    )
    assert low >= 0
