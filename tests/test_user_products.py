"""Creating, listing and deleting the user's own products.

YAZIO stores a product's nutrients per **one** base unit while every label a
caller reads them off states them per 100 g, and the create call answers with an
empty `204` — so neither the arithmetic nor the id the product was stored under
shows up in a round trip through this server. These tests therefore assert on
the draft that actually goes over the wire, which is the only place either is
visible.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yazio_mcp.server import build_server
from yazio_mcp.tools import products

# A label reading 250 kcal, 30 g carbohydrate, 12 g protein and 8 g fat per 100 g.
LABEL = {"energy_kcal": 250, "carb": 30, "protein": 12, "fat": 8}

# What the account already has, as GET /v22/products/{id} returns it.
OWN_PRODUCTS = {
    "MINE": {
        "id": "MINE",
        "name": "Grandma's bread",
        "producer": "Homemade",
        "base_unit": "g",
        "is_private": True,
        "nutrients": {"energy.energy": 2.5, "nutrient.carb": 0.3},
        "servings": [{"serving": "slice", "amount": 40.0}],
    }
}


@pytest.fixture
def submitted(monkeypatch):
    """Capture what the product tools send, with no network involved."""
    sent: dict[str, list] = {"drafts": [], "deleted": []}

    @asynccontextmanager
    async def fake_client(ctx):
        yield object()

    async def fake_create(*, client, body, **kwargs):
        sent["drafts"].append(body)
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    async def fake_delete(*, client, id, **kwargs):
        sent["deleted"].append(id)
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    async def fake_list(*, client, **kwargs):
        return SimpleNamespace(status_code=200, parsed=list(OWN_PRODUCTS), content=b"")

    async def fake_fetch_product(client, product_id):
        return OWN_PRODUCTS.get(product_id)

    monkeypatch.setattr(products, "yazio_client", fake_client)
    monkeypatch.setattr(products, "fetch_product", fake_fetch_product)
    monkeypatch.setattr(
        products.api_create_user_product, "asyncio_detailed", fake_create, raising=False
    )
    monkeypatch.setattr(
        products.api_delete_user_product, "asyncio_detailed", fake_delete, raising=False
    )
    monkeypatch.setattr(
        products.api_list_user_products, "asyncio_detailed", fake_list, raising=False
    )

    return sent


async def create(**overrides) -> None:
    arguments = {"name": "Grandma's bread", "nutrients": LABEL, **overrides}
    await build_server().call_tool("create_product", arguments)


async def refused(tool: str, arguments: dict) -> str:
    """Call a tool that is expected to refuse, and return the reason it gave.

    Validation runs before any network call, so the refusals below need no
    mocking. A call that passed validation would fail later on the absent
    credentials instead — never silently succeed — so each assertion is still
    reading the message it means to.
    """
    with pytest.raises(ToolError) as raised:
        await build_server().call_tool(tool, arguments)

    return str(raised.value)


def stored(draft) -> dict[str, float]:
    """The flat nutrient map a draft carries."""
    return draft.nutrients.to_dict()


@pytest.mark.asyncio
async def test_the_submitted_nutrients_are_per_base_unit(submitted):
    """The whole point: a 250 kcal per 100 g label is stored as 2.5 per gram."""
    await create()

    nutrients = stored(submitted["drafts"][0])
    assert nutrients["energy.energy"] == pytest.approx(2.5)
    assert nutrients["nutrient.carb"] == pytest.approx(0.3)
    assert nutrients["nutrient.protein"] == pytest.approx(0.12)
    assert nutrients["nutrient.fat"] == pytest.approx(0.08)


@pytest.mark.asyncio
async def test_a_label_stated_per_serving_is_divided_by_that_serving(submitted):
    """A 330 ml can of 139 kcal is 0.42 kcal per millilitre."""
    await create(
        nutrients={"energy_kcal": 139}, nutrients_per=330, base_unit="ml"
    )

    assert stored(submitted["drafts"][0])["energy.energy"] == pytest.approx(139 / 330)


@pytest.mark.asyncio
async def test_micronutrients_are_taken_in_milligrams(submitted):
    """The unit get_product reports them in, so a value can be copied across."""
    await create(nutrients={**LABEL, "mineral.calcium": 120})

    assert stored(submitted["drafts"][0])["mineral.calcium"] == pytest.approx(0.0012)


@pytest.mark.asyncio
async def test_the_draft_carries_an_id_of_its_own(submitted):
    """The create call answers 204 with no body, so the id has to be minted here."""
    await create()

    assert submitted["drafts"][0].id


@pytest.mark.asyncio
async def test_the_reported_product_id_is_the_one_submitted(submitted):
    """Reporting any other id would leave the caller unable to track the product."""
    result = await build_server().call_tool(
        "create_product", {"name": "Grandma's bread", "nutrients": LABEL}
    )

    assert structured(result)["product_id"] == submitted["drafts"][0].id


@pytest.mark.asyncio
async def test_a_new_product_is_private_by_default(submitted):
    """Publishing to YAZIO's shared database has to be asked for."""
    await create()

    assert submitted["drafts"][0].is_private is True


@pytest.mark.asyncio
async def test_a_product_can_be_offered_to_the_shared_database(submitted):
    await create(is_private=False)

    assert submitted["drafts"][0].is_private is False


@pytest.mark.asyncio
async def test_servings_are_submitted_as_yazio_serving_units(submitted):
    await create(servings=[{"serving": "slice", "amount": 40}])

    serving = submitted["drafts"][0].servings[0]
    assert serving.serving.value == "slice"
    assert serving.amount == 40.0


@pytest.mark.asyncio
async def test_an_omitted_producer_is_left_out_of_the_draft(submitted):
    """An empty string is a producer called ""; absent is absent."""
    await create()

    assert "producer" not in submitted["drafts"][0].to_dict()
    assert "category" not in submitted["drafts"][0].to_dict()


@pytest.mark.asyncio
async def test_a_nameless_product_is_refused():
    message = await refused(
        "create_product", {"name": "  ", "nutrients": LABEL}
    )
    assert "needs a name" in message


@pytest.mark.asyncio
async def test_a_product_without_energy_is_refused():
    """A food with no energy content is not something a diary can use."""
    message = await refused(
        "create_product", {"name": "Bread", "nutrients": {"carb": 30}}
    )
    assert "energy" in message


@pytest.mark.asyncio
async def test_an_unknown_base_unit_is_refused():
    """The field is a free-form string, and "oz" would be read back as grams."""
    message = await refused(
        "create_product",
        {"name": "Bread", "nutrients": LABEL, "base_unit": "oz"},
    )
    assert "base_unit must be one of g, ml" in message


@pytest.mark.asyncio
async def test_a_label_basis_that_cannot_be_true_is_refused():
    """Per-100 g numbers left at nutrients_per=1 would store it 100x too rich."""
    message = await refused(
        "create_product",
        {"name": "Bread", "nutrients": LABEL, "nutrients_per": 1},
    )
    assert "nutrients_per" in message


@pytest.mark.asyncio
async def test_a_dense_liquid_is_not_refused(submitted):
    """A millilitre of honey weighs 1.4 g, so volume gets no such check."""
    await create(
        nutrients={"energy_kcal": 304, "carb": 82},
        nutrients_per=100,
        base_unit="ml",
    )

    assert stored(submitted["drafts"][0])["nutrient.carb"] == pytest.approx(0.82)


@pytest.mark.asyncio
async def test_a_serving_unit_yazio_does_not_offer_is_refused():
    message = await refused(
        "create_product",
        {
            "name": "Bread",
            "nutrients": LABEL,
            "servings": [{"serving": "loaf", "amount": 500}],
        },
    )
    assert "not a serving unit" in message
    assert "slice" in message


@pytest.mark.asyncio
async def test_a_serving_without_a_usable_amount_is_refused():
    message = await refused(
        "create_product",
        {
            "name": "Bread",
            "nutrients": LABEL,
            "servings": [{"serving": "slice", "amount": 0}],
        },
    )
    assert "greater than zero" in message


@pytest.mark.asyncio
async def test_listing_expands_the_ids_into_products(submitted):
    """The endpoint returns bare ids, which a caller cannot choose between."""
    result = await build_server().call_tool("list_my_products", {})

    listed = structured(result)["products"]
    assert [product["name"] for product in listed] == ["Grandma's bread"]
    assert listed[0]["product_id"] == "MINE"


@pytest.mark.asyncio
async def test_listing_reports_energy_without_the_full_nutrient_table(submitted):
    """A table each would bury the names being chosen between."""
    listed = structured(await build_server().call_tool("list_my_products", {}))

    assert listed["products"][0]["energy_kcal_per_base_unit"] == pytest.approx(2.5)
    assert "nutrients_per_base_unit" not in listed["products"][0]


@pytest.mark.asyncio
async def test_deleting_removes_the_product(submitted):
    await build_server().call_tool("delete_product", {"product_id": "MINE"})

    assert submitted["deleted"] == ["MINE"]


@pytest.mark.asyncio
async def test_deleting_names_what_went(submitted):
    result = await build_server().call_tool("delete_product", {"product_id": "MINE"})

    assert structured(result)["name"] == "Grandma's bread"


@pytest.mark.asyncio
async def test_a_product_this_user_does_not_own_is_not_deleted(submitted):
    """The endpoint answers a foreign id the same way, so this is the only guard."""
    message = await refused("delete_product", {"product_id": "THEIRS"})

    assert "not one of your own products" in message
    assert submitted["deleted"] == []


def structured(result) -> dict:
    """The structured content of a tool result, whatever FastMCP wrapped it in."""
    return result[1] if isinstance(result, tuple) else result
