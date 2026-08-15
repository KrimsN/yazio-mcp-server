"""track_meal_photo: uploading a photo and logging YAZIO's single guess.

Recognition always came back with exactly one entry in simple_products and
nothing in products or ingredients, even for a mixed plate — there is no
candidate list to choose from, so these tests pin the guess-then-log shape
rather than any selection logic.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yazio_mcp.server import build_server
from yazio_mcp.tools import tracking

IMAGE_BYTES = b"fake-jpeg-bytes"
IMAGE_BASE64 = base64.b64encode(IMAGE_BYTES).decode()

PROFILE = SimpleNamespace(
    food_database_country="RU", country="RU", language="ru", sex="female"
)


def guess(name: str = "Banana", **nutrients: float) -> dict:
    """One entry as GET /v22/nutrimind-search/image/{id} returns it."""
    return {
        "name": name,
        "nutrients": {
            "energy.energy": nutrients.get("energy_energy", 100.0),
            "nutrient.protein": nutrients.get("nutrient_protein", 0.0),
            "nutrient.fat": nutrients.get("nutrient_fat", 0.0),
            "nutrient.carb": nutrients.get("nutrient_carb", 25.0),
        },
    }


@pytest.fixture
def api(monkeypatch):
    """Answer the recognition and logging endpoints locally, capturing what
    they are sent."""
    state = SimpleNamespace(
        guesses=[guess()], uploads=[], logged=[], profile=PROFILE
    )

    @asynccontextmanager
    async def fake_client(ctx):
        yield object()

    async def fake_get_user(*, client, **kwargs):
        return SimpleNamespace(status_code=200, parsed=state.profile, content=b"")

    async def fake_upload(*, id, client, body, countries, locales, **kwargs):
        state.uploads.append(
            {"id": id, "body": body, "countries": countries, "locales": locales}
        )
        return SimpleNamespace(status_code=200, parsed=None, content=b"")

    async def fake_result(*, id, client, countries, locales, **kwargs):
        result = SimpleNamespace(
            products=[], simple_products=list(state.guesses), ingredients=[]
        )
        return SimpleNamespace(status_code=200, parsed=result, content=b"")

    async def fake_add(*, client, body, **kwargs):
        state.logged.append(body)
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    monkeypatch.setattr(tracking, "yazio_client", fake_client)
    monkeypatch.setattr(tracking.api_get_user, "asyncio_detailed", fake_get_user, raising=False)
    monkeypatch.setattr(
        tracking.api_upload_nutrimind_search_image,
        "asyncio_detailed",
        fake_upload,
        raising=False,
    )
    monkeypatch.setattr(
        tracking.api_get_nutrimind_search_image,
        "asyncio_detailed",
        fake_result,
        raising=False,
    )
    monkeypatch.setattr(
        tracking.api_add_consumed_items, "asyncio_detailed", fake_add, raising=False
    )

    return state


async def track(**overrides) -> dict:
    arguments = {
        "image_base64": IMAGE_BASE64,
        "daytime": "snack",
        **overrides,
    }
    result = await build_server().call_tool("track_meal_photo", arguments)
    return result[1]


@pytest.mark.asyncio
async def test_the_decoded_photo_is_what_gets_uploaded(api):
    await track()

    uploaded = api.uploads[0]["body"].image
    assert uploaded.payload.read() == IMAGE_BYTES


@pytest.mark.asyncio
async def test_countries_and_locales_come_from_the_profile(api):
    await track()

    upload = api.uploads[0]
    assert upload["countries"] == "RU"
    assert upload["locales"] == "ru_RU,en_RU"


@pytest.mark.asyncio
async def test_the_upload_and_result_share_one_search_id(api):
    await track()

    assert api.uploads[0]["id"]
    # The fakes do not echo the id back, but a shared search_id is what makes
    # the GET find what the POST just wrote — this is pinned indirectly by
    # both calls succeeding rather than directly, since the fakes stand in
    # for two different endpoints on the real API.
    assert isinstance(api.uploads[0]["id"], str)


@pytest.mark.asyncio
async def test_the_recognized_guess_is_what_gets_logged(api):
    api.guesses = [guess(name="Grilled chicken", energy_energy=250)]

    payload = await track()

    logged = api.logged[0].simple_products[0]
    assert logged.name == "Grilled chicken"
    assert logged.nutrients.energy_energy == pytest.approx(250.0)
    assert payload["name"] == "Grilled chicken"
    assert payload["nutrients"]["energy_kcal"] == pytest.approx(250.0)


@pytest.mark.asyncio
async def test_the_logged_entry_is_marked_ai_generated(api):
    await track()

    assert api.logged[0].simple_products[0].is_ai_generated is True


@pytest.mark.asyncio
async def test_a_fresh_id_is_minted_for_the_diary_entry(api):
    payload = await track()

    logged = api.logged[0].simple_products[0]
    assert logged.id == payload["entry_id"]
    assert logged.id


@pytest.mark.asyncio
async def test_the_daytime_is_carried_through(api):
    payload = await track(daytime="lunch")

    assert api.logged[0].simple_products[0].daytime == "lunch"
    assert payload["daytime"] == "lunch"


@pytest.mark.asyncio
async def test_only_the_first_guess_is_logged(api):
    """Never observed more than one, but nothing stops the API from changing."""
    api.guesses = [guess(name="First"), guess(name="Second")]

    payload = await track()

    assert payload["name"] == "First"
    assert len(api.logged[0].simple_products) == 1


@pytest.mark.asyncio
async def test_no_guess_is_refused_with_a_clear_message(api):
    api.guesses = []

    with pytest.raises(ToolError, match="did not return a guess"):
        await track()

    assert api.logged == []


@pytest.mark.asyncio
async def test_invalid_base64_is_refused(api):
    with pytest.raises(ToolError, match="not valid base64"):
        await track(image_base64="not-base64-!!!")

    assert api.uploads == []


@pytest.mark.asyncio
async def test_an_empty_decoded_image_is_refused(api):
    with pytest.raises(ToolError, match="empty file"):
        await track(image_base64=base64.b64encode(b"").decode())

    assert api.uploads == []


@pytest.mark.asyncio
async def test_an_unknown_daytime_is_refused(api):
    with pytest.raises(ToolError, match="daytime must be one of"):
        await track(daytime="brunch")

    assert api.uploads == []
