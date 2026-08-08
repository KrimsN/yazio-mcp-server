"""The nutrient arithmetic of create_recipe and update_recipe.

YAZIO's recipe `nutrients` field holds the values for **one portion**, not for
the whole dish. Nothing in the API says so and nothing in a round trip through
this server reveals it — a write that submits the dish total and a read that
divides by the portion count cancel out, so the recipe only looks wrong in the
app. These tests therefore assert on the draft that actually goes over the
wire, which is the only place the error is visible.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yazio_mcp.server import build_server
from yazio_mcp.tools import recipes

# Per one base unit, as YAZIO stores product nutrients.
PRODUCTS = {
    "rice": {
        "name": "Reis",
        "base_unit": "g",
        "nutrients": {"energy.energy": 3.5, "nutrient.carb": 0.78},
        "servings": [{"serving": "cup", "amount": 180.0}],
    },
    "oil": {
        "name": "Olivenöl",
        "base_unit": "g",
        "nutrients": {"energy.energy": 8.84, "nutrient.fat": 1.0},
        "servings": [{"serving": "tablespoon", "amount": 15.0}],
    },
}

# 200 g rice + 50 g oil: 700 + 442 kcal, 156 g carb, 50 g fat.
INGREDIENTS = [
    {"product_id": "rice", "amount": 200},
    {"product_id": "oil", "amount": 50},
]


@pytest.fixture
def submitted(monkeypatch):
    """Capture the RecipeDraft a recipe tool posts, with no network involved."""
    drafts: list = []

    @asynccontextmanager
    async def fake_client(ctx):
        yield object()

    async def fake_fetch_product(client, product_id):
        return PRODUCTS.get(product_id)

    async def fake_write(*, client, body, **kwargs):
        drafts.append(body)
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    monkeypatch.setattr(recipes, "yazio_client", fake_client)
    monkeypatch.setattr(recipes, "fetch_product", fake_fetch_product)
    monkeypatch.setattr(recipes, "expect_written", lambda ctx, response, what: None)
    monkeypatch.setattr(
        recipes.api_create_user_recipe, "asyncio_detailed", fake_write, raising=False
    )
    monkeypatch.setattr(
        recipes.api_update_user_recipe, "asyncio_detailed", fake_write, raising=False
    )

    return drafts


def stored(draft) -> dict[str, float]:
    """The flat nutrient map a draft carries."""
    return draft.nutrients.to_dict()


async def create(**overrides) -> dict:
    arguments = {
        "name": "Reis mit Öl",
        "ingredients": INGREDIENTS,
        "portion_count": 2,
        **overrides,
    }
    return await build_server().call_tool("create_recipe", arguments)


@pytest.mark.asyncio
async def test_the_submitted_nutrients_are_per_portion(submitted):
    """The whole point: a 1142 kcal dish in three portions stores 380.67."""
    await create(portion_count=3)

    nutrients = stored(submitted[0])
    assert nutrients["energy.energy"] == pytest.approx(1142.0 / 3)
    assert nutrients["nutrient.carb"] == pytest.approx(156.0 / 3)
    assert nutrients["nutrient.fat"] == pytest.approx(50.0 / 3)


@pytest.mark.asyncio
async def test_a_single_portion_stores_the_whole_dish(submitted):
    await create(portion_count=1)

    assert stored(submitted[0])["energy.energy"] == pytest.approx(1142.0)


@pytest.mark.asyncio
async def test_the_default_portion_count_halves_the_dish(submitted):
    """The default is 2, so an unfixed server is wrong by at least a factor of two."""
    await create()

    assert stored(submitted[0])["energy.energy"] == pytest.approx(571.0)


@pytest.mark.asyncio
async def test_the_reported_totals_still_describe_the_whole_dish(submitted):
    """What the caller is told is unchanged; only what is stored moved."""
    result = await create(portion_count=3)
    payload = result[1]

    assert payload["nutrients_total"]["energy_kcal"] == pytest.approx(1142.0)
    assert payload["nutrients_per_portion"]["energy_kcal"] == pytest.approx(
        1142.0 / 3, abs=0.01
    )


def recipe(portion_count: int, energy: float) -> SimpleNamespace:
    """A stored recipe, with its per-portion nutrients as YAZIO holds them."""
    return SimpleNamespace(
        id="recipe-1",
        name="Reis mit Öl",
        portion_count=portion_count,
        instructions=["Kochen"],
        nutrients={"energy.energy": energy, "nutrient.carb": 52.0},
        servings=[
            {"product_id": "rice", "name": "Reis", "base_unit": "g", "amount": 200.0}
        ],
        is_yazio_recipe=False,
        is_pro_recipe=False,
        locale="de",
        image=None,
    )


@pytest.fixture
def owned(monkeypatch):
    """Make update_recipe's ownership check and recipe load resolve locally."""

    def install(existing) -> None:
        async def fake_list(*, client, **kwargs):
            return SimpleNamespace(status_code=200, parsed=["recipe-1"], content=b"")

        async def fake_load(ctx, client, recipe_id):
            return existing

        monkeypatch.setattr(
            recipes.api_list_user_recipes, "asyncio_detailed", fake_list, raising=False
        )
        monkeypatch.setattr(recipes, "expect_ok", lambda ctx, response, what: ["recipe-1"])
        monkeypatch.setattr(recipes, "_load_recipe", fake_load)

    return install


async def update(**overrides) -> dict:
    arguments = {"recipe_id": "recipe-1", **overrides}
    return await build_server().call_tool("update_recipe", arguments)


@pytest.mark.asyncio
async def test_raising_the_portion_count_rescales_the_stored_nutrients(
    submitted, owned
):
    """Splitting the same dish four ways instead of two halves each portion."""
    owned(recipe(portion_count=2, energy=571.0))

    result = await update(portion_count=4)

    assert stored(submitted[0])["energy.energy"] == pytest.approx(285.5)
    assert result[1]["nutrients_total"]["energy_kcal"] == pytest.approx(1142.0)


@pytest.mark.asyncio
async def test_lowering_the_portion_count_rescales_the_stored_nutrients(
    submitted, owned
):
    owned(recipe(portion_count=4, energy=285.5))

    await update(portion_count=2)

    assert stored(submitted[0])["energy.energy"] == pytest.approx(571.0)


@pytest.mark.asyncio
async def test_an_unrelated_edit_leaves_the_nutrients_alone(submitted, owned):
    owned(recipe(portion_count=2, energy=571.0))

    await update(name="Anderer Name")

    assert stored(submitted[0])["energy.energy"] == pytest.approx(571.0)


@pytest.mark.asyncio
async def test_new_ingredients_are_stored_per_portion(submitted, owned):
    owned(recipe(portion_count=2, energy=1.0))

    await update(ingredients=INGREDIENTS, portion_count=4)

    assert stored(submitted[0])["energy.energy"] == pytest.approx(1142.0 / 4)


def test_a_read_reports_the_stored_value_as_the_portion():
    """The stored field is per portion, so the dish is that times the count."""
    shaped = recipes._shape_recipe(recipe(portion_count=3, energy=380.0), brief=False)

    assert shaped["nutrients_per_portion"]["energy_kcal"] == pytest.approx(380.0)
    assert shaped["nutrients_total"]["energy_kcal"] == pytest.approx(1140.0)


def test_a_brief_read_reports_the_portion_too():
    shaped = recipes._shape_recipe(recipe(portion_count=3, energy=380.0), brief=True)

    assert shaped["nutrients_per_portion"]["energy_kcal"] == pytest.approx(380.0)


@pytest.mark.asyncio
async def test_a_write_and_a_read_agree(submitted):
    """Round-tripping a created recipe must reproduce the dish it was built from."""
    await create(portion_count=3)

    shaped = recipes._shape_recipe(
        SimpleNamespace(
            id="recipe-1",
            name="Reis mit Öl",
            portion_count=3,
            instructions=[],
            nutrients=stored(submitted[0]),
            servings=[],
            is_yazio_recipe=False,
            is_pro_recipe=False,
            locale="de",
            image=None,
        ),
        brief=False,
    )

    assert shaped["nutrients_total"]["energy_kcal"] == pytest.approx(1142.0)


class TestIngredientResolution:
    """The two smaller defects fixed alongside the per-portion arithmetic."""

    @pytest.fixture(autouse=True)
    def products(self, monkeypatch):
        async def fake_fetch_product(client, product_id):
            return PRODUCTS.get(product_id)

        monkeypatch.setattr(recipes, "fetch_product", fake_fetch_product)

    @pytest.mark.asyncio
    async def test_an_explicit_amount_drops_the_serving_label(self):
        """250 ml of a 330 ml can is not "1 can", and must not be stored as one."""
        resolved = await recipes._resolve_ingredient(
            None, {"product_id": "oil", "amount": 250, "serving": "tablespoon"}, 0
        )
        entry = resolved["serving_entry"]

        assert entry.amount == 250.0
        assert recipes.plain(entry.serving) is None
        assert recipes.plain(entry.serving_quantity) is None

    @pytest.mark.asyncio
    async def test_a_serving_alone_keeps_its_label(self):
        resolved = await recipes._resolve_ingredient(
            None, {"product_id": "oil", "serving": "tablespoon", "serving_quantity": 2}, 0
        )
        entry = resolved["serving_entry"]

        assert entry.amount == 30.0
        assert entry.serving == "tablespoon"
        assert entry.serving_quantity == 2.0

    @pytest.mark.asyncio
    async def test_a_zero_amount_is_refused(self):
        with pytest.raises(ToolError, match="greater than zero"):
            await recipes._resolve_ingredient(
                None, {"product_id": "rice", "amount": 0}, 0
            )

    @pytest.mark.asyncio
    async def test_a_negative_amount_is_refused(self):
        with pytest.raises(ToolError, match="greater than zero"):
            await recipes._resolve_ingredient(
                None, {"product_id": "rice", "amount": -100}, 1
            )

    @pytest.mark.asyncio
    async def test_the_nutrients_are_scaled_to_the_amount(self):
        resolved = await recipes._resolve_ingredient(
            None, {"product_id": "rice", "amount": 200}, 0
        )

        assert resolved["nutrients"]["energy.energy"] == pytest.approx(700.0)
