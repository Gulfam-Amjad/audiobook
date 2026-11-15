"""Settings must be validated against each provider's real capabilities."""

from __future__ import annotations

from app.providers import registry
from app.services.generation import (
    JobSettings,
    describe_image_compatibility,
    match_to_provider,
)


def setup_function() -> None:
    registry.reset_cache()


def test_accepted_settings_pass_unchanged() -> None:
    result = match_to_provider(
        "mock", JobSettings(model_id="mock-v1", duration_s=5, resolution="720p", aspect_ratio="16:9")
    )
    assert result.ok
    assert not result.was_adjusted
    assert result.settings.duration_s == 5


def test_unsupported_duration_is_snapped_to_nearest() -> None:
    result = match_to_provider(
        "mock", JobSettings(model_id="mock-v1", duration_s=7, resolution="720p")
    )
    assert result.ok
    assert result.settings.duration_s == 8, "nearest of (5, 8, 10)"
    assert any(a.field_name == "duration" for a in result.adjustments)
    assert "7s -> 8s" in result.summary()


def test_adjustment_can_be_refused_instead_of_applied() -> None:
    result = match_to_provider(
        "mock",
        JobSettings(model_id="mock-fast", duration_s=10, resolution="720p"),
        allow_adjustment=False,
    )
    assert not result.ok
    assert "does not support 10s" in result.summary()


def test_resolution_falls_back_to_nearest_supported() -> None:
    result = match_to_provider(
        "mock", JobSettings(model_id="mock-fast", duration_s=5, resolution="1080p")
    )
    assert result.ok
    assert result.settings.resolution == "720p", "mock-fast tops out at 720p"


def test_seed_is_dropped_for_models_that_ignore_it() -> None:
    result = match_to_provider(
        "mock", JobSettings(model_id="mock-fast", duration_s=5, resolution="720p", seed=42)
    )
    assert result.settings.seed is None
    assert any(a.field_name == "seed" for a in result.adjustments)


def test_undeclared_extra_settings_are_stripped() -> None:
    result = match_to_provider(
        "mock",
        JobSettings(
            model_id="mock-v1",
            duration_s=5,
            resolution="720p",
            extra={"camera_move": "orbit", "nonsense": 1},
        ),
    )
    assert result.settings.extra == {}, "fields the model never declared must not be sent"
    assert any(a.field_name == "extra settings" for a in result.adjustments)


def test_unknown_provider_is_reported_not_guessed() -> None:
    result = match_to_provider("does-not-exist", JobSettings())
    assert not result.ok
    assert "not available" in result.summary()


def test_model_is_chosen_when_unpinned() -> None:
    result = match_to_provider("mock", JobSettings(model_id="", duration_s=10))
    assert result.ok
    assert result.settings.model_id == "mock-v1", "mock-fast cannot do 10s"


def test_cost_estimate_reflects_duration_and_resolution() -> None:
    adapter = registry.get_adapter("mock")
    assert adapter is not None
    caps = adapter.capabilities()
    model = caps.model("mock-v1")
    assert model is not None
    # The mock prices in units rather than dollars, which is what the credit
    # maths consumes; the USD figure stays zero because it costs nothing.
    assert model.price.units_per_second["720p"] == 2.0


def test_image_compatibility_reports_each_violation() -> None:
    problems = describe_image_compatibility(
        "mock", width=100, height=100, size_bytes=50 * 1024 * 1024, extension=".gif"
    )
    joined = " ".join(problems)
    assert "accepts" in joined and ".gif" in joined
    assert "MB" in joined
    assert "at least" in joined


def test_image_compatibility_passes_a_good_file() -> None:
    problems = describe_image_compatibility(
        "mock", width=1920, height=1080, size_bytes=2 * 1024 * 1024, extension=".jpg"
    )
    assert problems == []


def test_extreme_aspect_ratio_is_rejected() -> None:
    problems = describe_image_compatibility(
        "mock", width=4000, height=500, size_bytes=1024, extension=".jpg"
    )
    assert any("wider" in p for p in problems)
