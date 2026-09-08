"""The two ids a favourite carries, and which one each call is addressed by.

A favourite entry has an id of its own: `PUT /v22/user/favorites/recipes` takes
it in the body next to the `recipe_id`, and `DELETE /v22/user/favorites/{id}`
removes the entry by *that* id rather than by the recipe. A caller never holds
it — a model has the recipe's id — so both tools are really the translation
between the two, and these tests assert on the request that goes over the wire,
which is the only place a confusion between them is visible.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.models import FavoriteRecipe

from yazio_mcp.server import build_server
from yazio_mcp.tools import recipes

RECIPE = SimpleNamespace(id="recipe-1", name="Reis mit Öl", portion_count=4)


def entry(favorite_id: str, recipe_id: str, portions: float = 1.0) -> FavoriteRecipe:
    """One item as GET /v22/user/favorites/recipe returns it."""
    return FavoriteRecipe(id=favorite_id, recipe_id=recipe_id, portion_count=portions)


@pytest.fixture
def api(monkeypatch):
    """Answer the favourite endpoints locally, capturing what they are sent."""
    state = SimpleNamespace(favorites=[], added=[], removed=[], recipe=RECIPE)

    @asynccontextmanager
    async def fake_client(ctx):
        yield object()

    async def fake_load_recipe(ctx, client, recipe_id):
        return state.recipe

    async def fake_list(*, client, **kwargs):
        return SimpleNamespace(
            status_code=200, parsed=list(state.favorites), content=b""
        )

    async def fake_add(*, client, body, **kwargs):
        state.added.append(body)
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    async def fake_remove(*, client, id, **kwargs):
        state.removed.append(id)
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    monkeypatch.setattr(recipes, "yazio_client", fake_client)
    monkeypatch.setattr(recipes, "_load_recipe", fake_load_recipe)
    monkeypatch.setattr(
        recipes.api_list_favorite_recipes, "asyncio_detailed", fake_list, raising=False
    )
    monkeypatch.setattr(
        recipes.api_add_favorite_recipe, "asyncio_detailed", fake_add, raising=False
    )
    monkeypatch.setattr(
        recipes.api_remove_favorite, "asyncio_detailed", fake_remove, raising=False
    )

    return state


async def favorite(**overrides) -> dict:
    arguments = {"recipe_id": "recipe-1", **overrides}
    result = await build_server().call_tool("favorite_recipe", arguments)
    return result[1]


async def unfavorite(**overrides) -> dict:
    arguments = {"recipe_id": "recipe-1", **overrides}
    result = await build_server().call_tool("unfavorite_recipe", arguments)
    return result[1]


@pytest.mark.asyncio
async def test_the_recipe_id_is_what_is_favourited(api):
    await favorite()

    assert api.added[0].recipe_id == "recipe-1"


@pytest.mark.asyncio
async def test_a_new_favourite_is_given_an_id_of_its_own(api):
    """The entry's id is minted client-side, as create_recipe mints a recipe's."""
    await favorite()

    assert api.added[0].id != "recipe-1"
    assert api.added[0].id


@pytest.mark.asyncio
async def test_the_portion_count_defaults_to_the_recipes_own(api):
    payload = await favorite()

    assert api.added[0].portion_count == pytest.approx(4.0)
    assert payload["portion_count"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_an_explicit_portion_count_is_what_is_stored(api):
    await favorite(portion_count=2.5)

    assert api.added[0].portion_count == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_favouriting_again_reuses_the_stored_id(api):
    """Otherwise the same recipe ends up in the list twice, under two ids."""
    api.favorites = [entry("fav-1", "recipe-1", 4.0)]

    payload = await favorite(portion_count=2)

    assert api.added[0].id == "fav-1"
    assert api.added[0].portion_count == pytest.approx(2.0)
    assert payload["was_already_a_favorite"] is True


@pytest.mark.asyncio
async def test_another_recipes_favourite_does_not_lend_its_id(api):
    api.favorites = [entry("fav-other", "recipe-2")]

    payload = await favorite()

    assert api.added[0].id != "fav-other"
    assert payload["was_already_a_favorite"] is False


@pytest.mark.asyncio
async def test_an_unknown_recipe_is_refused(api):
    """The endpoint answers 204 to any id, so a typo has to be caught here."""
    api.recipe = None

    with pytest.raises(ToolError, match="no recipe found"):
        await favorite()

    assert api.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("portions", [0, -1])
async def test_a_portion_count_of_zero_or_less_is_refused(api, portions):
    with pytest.raises(ToolError, match="greater than zero"):
        await favorite(portion_count=portions)

    assert api.added == []


@pytest.mark.asyncio
async def test_removal_is_by_the_favourites_own_id(api):
    api.favorites = [entry("fav-1", "recipe-1")]

    payload = await unfavorite()

    assert api.removed == ["fav-1"]
    assert payload["removed"] == 1


@pytest.mark.asyncio
async def test_every_entry_for_the_recipe_is_removed(api):
    """A second entry for the same recipe would keep it in the list."""
    api.favorites = [
        entry("fav-1", "recipe-1"),
        entry("fav-2", "recipe-2"),
        entry("fav-3", "recipe-1"),
    ]

    payload = await unfavorite()

    assert api.removed == ["fav-1", "fav-3"]
    assert payload["removed"] == 2


@pytest.mark.asyncio
async def test_unfavouriting_a_recipe_that_is_not_a_favourite_is_refused(api):
    api.favorites = [entry("fav-other", "recipe-2")]

    with pytest.raises(ToolError, match="not one of your favourites"):
        await unfavorite()

    assert api.removed == []


@pytest.mark.asyncio
async def test_the_refusal_names_the_tool_that_lists_the_ids(api):
    with pytest.raises(ToolError, match="get_favorite_recipes"):
        await unfavorite()


@pytest.mark.asyncio
async def test_the_listing_keeps_the_two_ids_apart(api):
    """`id` in the payload is the favourite's; the tools take `recipe_id`."""
    api.favorites = [entry("fav-1", "recipe-1", 2.0)]

    result = await build_server().call_tool("get_favorite_recipes", {})
    listed = result[1]["favorites"][0]

    assert listed["recipe_id"] == "recipe-1"
    assert listed["favorite_id"] == "fav-1"
    assert listed["portion_count"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_a_favourite_survives_the_round_trip(api):
    """What favorite_recipe writes is what get_favorite_recipes reads back."""
    await favorite(portion_count=3)
    api.favorites = [
        entry(api.added[0].id, api.added[0].recipe_id, api.added[0].portion_count)
    ]

    result = await build_server().call_tool("get_favorite_recipes", {})
    listed = result[1]["favorites"][0]

    assert listed["recipe_id"] == "recipe-1"
    assert listed["portion_count"] == pytest.approx(3.0)

    await unfavorite()

    assert api.removed == [api.added[0].id]
