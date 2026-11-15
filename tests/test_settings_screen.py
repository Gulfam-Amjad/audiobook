"""The Settings screen, exercised through real HTTP requests.

The spending section is the only place paid generation can be armed, so
these go through the actual routes rather than the service layer - a guard
that exists in the service but is bypassed by the form is no guard at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account
from app.services import provider_sync, settings_store

pytestmark = pytest.mark.usefixtures("data_root")


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An app instance without the lifespan, so no scheduler is started."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def flash(response) -> str:
    return response.headers.get("X-AVM-Flash", "")


async def test_the_settings_page_renders(client: AsyncClient) -> None:
    response = await client.get("/settings")
    assert response.status_code == 200
    assert "Spending" in response.text


async def test_a_fresh_install_shows_that_nothing_can_be_charged(
    client: AsyncClient,
) -> None:
    response = await client.get("/settings")
    assert "Nothing can be charged" in response.text


async def test_provider_keys_are_never_rendered_into_the_page(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentials live in .env precisely so they cannot reach a template."""
    from app import config

    monkeypatch.setenv("AVM_RUNWAY_API_KEY", "sk-should-never-be-rendered")
    config.get_settings.cache_clear()

    response = await client.get("/settings")
    assert "sk-should-never-be-rendered" not in response.text
    config.get_settings.cache_clear()


async def test_turning_paid_generation_on_warns_that_accounts_still_need_authorising(
    client: AsyncClient, session: AsyncSession
) -> None:
    response = await client.post(
        "/settings/spending",
        data={"paid_enabled": "true", "monthly_spend_cap_usd": "5"},
    )
    assert response.status_code == 200
    assert "authorised" in await flash(response)
    assert await settings_store.get(session, "paid_enabled") is True


async def test_a_cap_of_zero_with_paid_on_is_called_out(client: AsyncClient) -> None:
    response = await client.post(
        "/settings/spending",
        data={"paid_enabled": "true", "monthly_spend_cap_usd": "0"},
    )
    assert "still be refused" in await flash(response)


async def test_a_negative_cap_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/settings/spending",
        data={"paid_enabled": "false", "monthly_spend_cap_usd": "-1"},
    )
    assert response.status_code == 400


async def test_an_implausibly_large_cap_is_refused(client: AsyncClient) -> None:
    """A fat-fingered extra zero should not become a real bill."""
    response = await client.post(
        "/settings/spending",
        data={"paid_enabled": "true", "monthly_spend_cap_usd": "50000"},
    )
    assert response.status_code == 400
    assert "ceiling" in response.json()["detail"]


async def test_turning_paid_generation_off_withdraws_authorisations(
    client: AsyncClient, session: AsyncSession
) -> None:
    await provider_sync.sync_providers(session)
    await session.commit()
    # Held as a plain int: the routes write through their own sessions, and
    # expiring this one would make reading account.id trigger a synchronous
    # refresh from async context.
    account_id = (
        await session.execute(select(Account.id).where(Account.provider_key == "mock"))
    ).scalar_one()

    await client.post(
        "/settings/spending", data={"paid_enabled": "true", "monthly_spend_cap_usd": "5"}
    )
    await client.post(f"/accounts/{account_id}/paid", data={"authorized": "true"})
    session.expire_all()
    account = await session.get(Account, account_id)
    assert account is not None and account.paid_authorized

    response = await client.post(
        "/settings/spending", data={"paid_enabled": "false", "monthly_spend_cap_usd": "5"}
    )

    session.expire_all()
    account = await session.get(Account, account_id)
    assert account is not None and not account.paid_authorized
    assert "withdrawn" in await flash(response)


async def test_omitting_the_paid_checkbox_switches_it_off(
    client: AsyncClient, session: AsyncSession
) -> None:
    """An unchecked HTML checkbox is absent, not false - it must still mean off."""
    await client.post(
        "/settings/spending", data={"paid_enabled": "true", "monthly_spend_cap_usd": "5"}
    )
    assert await settings_store.get(session, "paid_enabled") is True

    await client.post("/settings/spending", data={"monthly_spend_cap_usd": "5"})
    assert await settings_store.get(session, "paid_enabled") is False


# ---------------------------------------------------------------------------
# General settings
# ---------------------------------------------------------------------------


def general_form(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "default_duration_seconds": 5,
        "default_resolution": "720p",
        "default_aspect_ratio": "16:9",
        "daily_target_minutes": 20,
        "poll_interval_seconds": 10,
        "max_attempts": 3,
        "job_timeout_minutes": 30,
    }
    data.update(overrides)
    return data


async def test_general_settings_save(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.post(
        "/settings/general", data=general_form(default_duration_seconds=8)
    )
    assert response.status_code == 200
    assert await settings_store.get(session, "default_duration_seconds") == 8


async def test_an_unknown_resolution_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/settings/general", data=general_form(default_resolution="9000p")
    )
    assert response.status_code == 400


async def test_an_absurd_clip_length_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/settings/general", data=general_form(default_duration_seconds=600)
    )
    assert response.status_code == 400


async def test_polling_faster_than_the_floor_is_refused(client: AsyncClient) -> None:
    """Hammering a status endpoint is the fastest way to get rate limited."""
    response = await client.post(
        "/settings/general", data=general_form(poll_interval_seconds=1)
    )
    assert response.status_code == 400


async def test_an_output_folder_that_cannot_be_written_is_refused(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/settings/general",
        data=general_form(output_dir_override="Q:\\nonexistent\\path"),
    )
    assert response.status_code == 400


async def test_a_relative_output_folder_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/settings/general", data=general_form(output_dir_override="output")
    )
    assert response.status_code == 400
    assert "absolute" in response.json()["detail"].lower()


async def test_a_writable_output_folder_is_accepted(
    client: AsyncClient, session: AsyncSession, tmp_path
) -> None:
    target = tmp_path / "videos"
    response = await client.post(
        "/settings/general", data=general_form(output_dir_override=str(target))
    )
    assert response.status_code == 200
    assert target.exists(), "the folder should be created when it is accepted"
    assert await settings_store.get(session, "output_dir_override") == str(target)


async def test_the_write_test_leaves_no_file_behind(tmp_path) -> None:
    from app.services.paths import validate_output_dir

    target = tmp_path / "probe"
    assert validate_output_dir(str(target)) is None
    assert list(target.iterdir()) == []
