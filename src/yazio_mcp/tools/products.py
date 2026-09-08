"""Finding products in YAZIO's food database, and adding one that is missing.

Most of this is a translation of a search or a lookup. `create_product` is not:
YAZIO stores a product's nutrients per *one* base unit, while every label a
caller reads them off states them per 100 g or per portion, so the tool takes
the basis the numbers came in and divides by it. The nutrient names go through
the same translation in reverse — see `nutrients.flatten_nutrients`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk import AuthenticatedClient
from yazio_sdk.api.products import create_user_product as api_create_user_product
from yazio_sdk.api.products import delete_user_product as api_delete_user_product
from yazio_sdk.api.products import get_product as api_get_product
from yazio_sdk.api.products import (
    list_suggested_products as api_list_suggested_products,
)
from yazio_sdk.api.products import list_user_products as api_list_user_products
from yazio_sdk.api.products import search_products as api_search_products
from yazio_sdk.api.user import get_user as api_get_user
from yazio_sdk.models import (
    ServingUnit,
    UserProductDraft,
    UserProductDraftNutrients,
    UserProductDraftServingsItem,
)

from ..common import plain, resolve_date, resolve_daytime, round_floats
from ..nutrients import ENERGY_KEY, MACRO_KEYS, flatten_nutrients, group_nutrients
from ..session import expect_ok, expect_written, yazio_client

# Used only if the profile does not say. Search rejects a blank country outright,
# so there has to be some value to fall back on.
_FALLBACK_COUNTRY = "DE"
_FALLBACK_SEX = "male"

# What YAZIO measures a product in. Anything else is refused before sending:
# the field is a free-form string in the API, and a product stored in "oz" would
# be scaled as though it were grams by everything that reads it back.
_BASE_UNITS = ("g", "ml")

# The portions a user-created product may offer. YAZIO limits these to a fixed
# list, unlike the free-form serving strings its own database carries.
_SERVING_UNITS = tuple(unit.value for unit in ServingUnit)

# How many of the user's own products to expand per call. Listing returns bare
# ids, so each one costs a request.
_MAX_EXPANDED = 25


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def search_products(
        ctx: Context,
        query: str,
        limit: int = 20,
        countries: str | None = None,
    ) -> dict[str, Any]:
        """Search YAZIO's food database by name or barcode.

        Accepts free text ("greek yoghurt") as well as a scanned EAN barcode.
        Each result carries the product_id needed to track it, its default
        serving, and per-serving nutrients.

        Results are ranked for the user's own country and language unless a
        different country is named.

        Args:
            query: Product name or barcode to search for.
            limit: Maximum number of results to return.
            countries: Comma-separated country codes to search instead of the
                user's own, such as "US" or "GB,IE".
        """
        if not query.strip():
            raise ToolError("query must not be empty")

        async with yazio_client(ctx) as client:
            # The API rejects a search with a blank `sex` or `countries`, so
            # both have to be supplied even though neither is something a caller
            # should have to think about. Taking them from the profile keeps
            # results ranked the way the app would rank them.
            profile_response = await api_get_user.asyncio_detailed(client=client)
            profile = expect_ok(ctx, profile_response, "read your search region")

            response = await api_search_products.asyncio_detailed(
                client=client,
                query=query.strip(),
                sex=plain(profile.sex) or _FALLBACK_SEX,
                countries=countries or _search_country(profile),
                locales=_search_locales(profile),
            )
            results = expect_ok(ctx, response, f"search for '{query}'")

        products = [_shape_search_result(result) for result in results[:limit]]
        return round_floats(
            {
                "query": query,
                "returned": len(products),
                "total_matches": len(results),
                "products": products,
            }
        )

    @mcp.tool()
    async def get_product(ctx: Context, product_id: str) -> dict[str, Any]:
        """Look up one product's full detail, including its serving options.

        Use this before tracking when you need to know which serving units a
        product supports, or to get its complete nutrient breakdown rather than
        just the four macros that search returns.

        Args:
            product_id: The product's UUID, as returned by search_products.
        """
        async with yazio_client(ctx) as client:
            detail = await fetch_product(client, product_id)

        if detail is None:
            raise ToolError(f"no product found with id {product_id}")

        return round_floats(_shape_product(detail))

    @mcp.tool()
    async def get_suggested_products(
        ctx: Context,
        daytime: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """List the products YAZIO suggests for a given meal.

        These are drawn from what this user usually eats at that time of day, so
        they are the fastest route to logging a repeat meal: each suggestion
        already carries the amount and serving to track.

        Args:
            daytime: One of breakfast, lunch, dinner, snack.
            date: Day to get suggestions for as YYYY-MM-DD. Defaults to today.
        """
        slot = resolve_daytime(daytime)
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_list_suggested_products.asyncio_detailed(
                client=client, daytime=slot, date=day
            )
            suggestions = expect_ok(ctx, response, f"load {slot} suggestions")

            # Suggestions are bare ids; resolving them here is what makes the
            # result usable, since a caller cannot pick between UUIDs.
            details = [
                await fetch_product(client, _suggestion_id(item))
                for item in suggestions
            ]

        products = []
        for suggestion, detail in zip(suggestions, details, strict=True):
            entry = {
                "product_id": _suggestion_id(suggestion),
                "amount": plain(suggestion.amount),
                "serving": plain(suggestion.serving),
                "serving_quantity": plain(suggestion.serving_quantity),
            }
            if detail is not None:
                entry["name"] = detail.get("name")
                entry["producer"] = detail.get("producer")
            products.append(entry)

        return round_floats({"date": day, "daytime": slot, "products": products})

    @mcp.tool()
    async def create_product(
        ctx: Context,
        name: str,
        nutrients: dict[str, float],
        nutrients_per: float = 100,
        base_unit: str = "g",
        producer: str | None = None,
        category: str | None = None,
        servings: list[dict[str, Any]] | None = None,
        is_private: bool = True,
    ) -> dict[str, Any]:
        """Add a product that YAZIO's database does not have.

        For a food search_products cannot find: something homemade, a local
        brand, a supplement. The product can be logged straight away with the
        product_id this returns, which is also the only id the creation reports
        — the API answers it with an empty body.

        Nutrients are given as the packaging states them, with `nutrients_per`
        saying what they are stated per:

            nutrients={"energy_kcal": 250, "carb": 30, "protein": 12, "fat": 8},
            nutrients_per=100

        Energy is in kilocalories and macros in grams. Any further nutrient has
        to be named by its exact YAZIO key and given in milligrams, as
        get_product reports it for an existing product: "mineral.calcium",
        "vitamin.b12". Energy is required; everything else is optional.

        YAZIO has no endpoint for editing a product, so what is submitted here
        is final. Correcting a mistake means deleting the product and creating
        it again, which leaves anything already logged pointing at the deleted
        one.

        Args:
            name: What the product is called.
            nutrients: The label's nutrient table, as described above.
            nutrients_per: How many base units that table describes. A label
                reading "per 100 g" is 100, one reading "per 330 ml can" is 330.
            base_unit: "g" for solids, "ml" for liquids.
            producer: The brand or manufacturer, if the food has one.
            category: YAZIO's own category for this kind of food, as get_product
                reports it for a similar product. Omitted if not given.
            servings: Ready-made portions, such as
                [{"serving": "package", "amount": 330}], the amount being in
                base units. Only YAZIO's fixed serving units are accepted.
            is_private: Keep the product to this account. Setting it false
                offers the product to YAZIO's shared database, where other
                people will see it.
        """
        if not name.strip():
            raise ToolError("a product needs a name")

        unit = base_unit.strip().lower()
        if unit not in _BASE_UNITS:
            raise ToolError(
                f"base_unit must be one of {', '.join(_BASE_UNITS)}; got '{base_unit}'"
            )

        # Stored per one base unit, so the label's basis is divided out here.
        per_base_unit = flatten_nutrients(nutrients, float(nutrients_per))
        if ENERGY_KEY not in per_base_unit:
            raise ToolError(
                "a product needs its energy content: give energy_kcal in "
                "kilocalories per nutrients_per units"
            )
        _refuse_impossible_macros(per_base_unit, unit)

        draft = UserProductDraft(
            # YAZIO answers 204 with an empty body, so the id has to be minted
            # here — it is the only way the caller learns what to track.
            id=str(uuid.uuid4()).upper(),
            name=name.strip(),
            base_unit=unit,
            is_private=bool(is_private),
            nutrients=UserProductDraftNutrients.from_dict(per_base_unit),
            servings=_draft_servings(servings or []),
        )
        if producer is not None:
            draft.producer = producer.strip()
        if category is not None:
            draft.category = category.strip()

        async with yazio_client(ctx) as client:
            response = await api_create_user_product.asyncio_detailed(
                client=client, body=draft
            )
            expect_written(ctx, response, f"create the product '{draft.name}'")

        return round_floats(
            {
                "created": True,
                "product_id": draft.id,
                "name": draft.name,
                "producer": plain(draft.producer),
                "category": plain(draft.category),
                "base_unit": unit,
                "is_private": draft.is_private,
                "servings": [
                    {"serving": item.serving.value, "amount": item.amount}
                    for item in draft.servings
                ],
                "nutrients_per_base_unit": group_nutrients(per_base_unit),
            }
        )

    @mcp.tool()
    async def list_my_products(
        ctx: Context, limit: int = _MAX_EXPANDED
    ) -> dict[str, Any]:
        """List the products this user added to the database.

        Args:
            limit: How many products to load in full. The total count is always
                reported.
        """
        async with yazio_client(ctx) as client:
            response = await api_list_user_products.asyncio_detailed(client=client)
            product_ids = expect_ok(ctx, response, "list your products")

            wanted = product_ids[: max(0, min(limit, _MAX_EXPANDED))]
            details = await asyncio.gather(
                *(fetch_product(client, product_id) for product_id in wanted)
            )

        found = [detail for detail in details if detail is not None]
        return round_floats(
            {
                "total": len(product_ids),
                "returned": len(found),
                "products": [_shape_product(detail, brief=True) for detail in found],
            }
        )

    @mcp.tool()
    async def delete_product(ctx: Context, product_id: str) -> dict[str, Any]:
        """Delete a product this user added.

        Diary entries that already logged it are left alone and go on naming it;
        use untrack_item to remove those.

        Args:
            product_id: The product's UUID, from list_my_products or
                create_product.
        """
        async with yazio_client(ctx) as client:
            # The delete endpoint answers a product this user does not own the
            # same way it answers one they do, so this membership check is the
            # only thing that turns "not yours" into an explanation.
            response = await api_list_user_products.asyncio_detailed(client=client)
            owned_ids = expect_ok(ctx, response, "list your products")
            if product_id not in owned_ids:
                raise ToolError(
                    f"product {product_id} is not one of your own products, so it "
                    "can't be deleted. Only products you created can be — check "
                    "list_my_products for the ids you own."
                )

            # Read it before it is gone, so the answer can name what went.
            detail = await fetch_product(client, product_id)

            response = await api_delete_user_product.asyncio_detailed(
                client=client, id=product_id
            )
            expect_written(ctx, response, f"delete product {product_id}")

        return {
            "deleted": True,
            "product_id": product_id,
            "name": (detail or {}).get("name"),
        }


async def fetch_product(
    client: AuthenticatedClient, product_id: str
) -> dict[str, Any] | None:
    """Fetch one product as a plain dict.

    Returns None for a 404 so that callers resolving ids in bulk can tolerate
    one product having been deleted, rather than losing the whole diary day to
    a single dead reference.

    The requested id is folded into the result because the response body does
    not carry one — the caller already knows it, but everything downstream reads
    the product as a self-contained object.
    """
    try:
        response = await api_get_product.asyncio_detailed(
            client=client, id=product_id
        )
    except httpx.HTTPError as exc:
        raise ToolError(
            f"could not reach YAZIO to load product {product_id}: {exc}"
        ) from exc

    if response.status_code == 404:
        return None
    if not 200 <= response.status_code < 300:
        raise ToolError(
            f"YAZIO returned {response.status_code} while loading product {product_id}"
        )
    if response.parsed is None:
        raise ToolError(f"YAZIO returned an unreadable body for product {product_id}")

    return {"id": product_id, **(plain(response.parsed) or {})}


def _search_country(profile: Any) -> str:
    """Pick the country whose food database the user actually eats from.

    YAZIO keeps this separate from the account country: someone living abroad
    may still want the database of their home country.
    """
    country = plain(profile.food_database_country) or plain(profile.country)
    return str(country or _FALLBACK_COUNTRY).upper()


def _search_locales(profile: Any) -> str:
    """Build the locale ranking hint from the user's language and country."""
    language = str(plain(profile.language) or "en").lower()
    country = _search_country(profile)
    return f"{language}_{country},en_{country}"


def _suggestion_id(suggestion: Any) -> str:
    product_id = plain(suggestion.product_id)
    if not isinstance(product_id, str):
        raise ToolError("YAZIO returned a suggestion without a product id")
    return product_id


def _shape_search_result(result: Any) -> dict[str, Any]:
    return {
        "product_id": plain(result.product_id),
        "name": plain(result.name),
        "producer": plain(result.producer),
        "is_verified": plain(result.is_verified),
        "base_unit": plain(result.base_unit),
        "serving": plain(result.serving),
        "serving_quantity": plain(result.serving_quantity),
        "amount": plain(result.amount),
        "nutrients_per_serving": group_nutrients(plain(result.nutrients) or {}),
        "language": plain(result.language),
        "countries": plain(result.countries),
    }


def _draft_servings(servings: list[Any]) -> list[UserProductDraftServingsItem]:
    """Validate the ready-made portions a new product offers.

    Unlike the free-form serving strings the product database carries, a
    user-created product may only use YAZIO's fixed list — anything else is
    accepted by the API and then shows up as a portion the app cannot name.
    """
    entries = []
    for index, item in enumerate(servings):
        if not isinstance(item, dict):
            raise ToolError(
                f"serving {index + 1} must be an object with a serving and an amount"
            )

        try:
            serving = ServingUnit(str(item.get("serving") or "").strip().lower())
        except ValueError as exc:
            raise ToolError(
                f"serving {index + 1}: '{item.get('serving')}' is not a serving unit "
                f"YAZIO offers. Use one of {', '.join(_SERVING_UNITS)}."
            ) from exc

        amount = item.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ToolError(
                f"serving {index + 1} ({serving.value}) needs an amount in base "
                f"units; got {amount!r}"
            )
        if amount <= 0:
            raise ToolError(
                f"serving {index + 1} ({serving.value}): amount must be greater "
                f"than zero; got {amount}"
            )

        entries.append(
            UserProductDraftServingsItem(serving=serving, amount=float(amount))
        )

    return entries


def _refuse_impossible_macros(per_base_unit: dict[str, float], base_unit: str) -> None:
    """Refuse a nutrient table that weighs more than the food it describes.

    A gram of anything cannot hold more than a gram of carbohydrate, protein and
    fat together, which catches the one mistake here that is easy to make and
    invisible afterwards: handing over a label's per-100 g numbers with
    `nutrients_per` left at 1, storing a product a hundred times too rich.

    Solids only. A millilitre of honey weighs about 1.4 g, so the same reasoning
    does not hold by volume.
    """
    if base_unit != "g":
        return

    macros = sum(per_base_unit.get(key, 0.0) for key in MACRO_KEYS)
    if macros > 1.0:
        raise ToolError(
            f"the macros come to {macros:.1f} g per gram of product, which no food "
            "can be — check that nutrients_per matches the basis the nutrients "
            "are stated in"
        )


def _shape_product(detail: dict[str, Any], brief: bool = False) -> dict[str, Any]:
    """Reshape a raw product payload, keeping anything we do not recognise.

    The spec models this endpoint loosely — the SDK keeps anything it does not
    name in `additional_properties`, which `plain` folds back in — so this pulls
    out the fields the app is known to use and passes the remainder through
    under `extra` rather than dropping whatever the spec has yet to catch up on.

    The brief form is used when listing the user's own products, where a full
    nutrient table each would bury the names being chosen between.
    """
    known = {
        "id",
        "name",
        "producer",
        "base_unit",
        "is_verified",
        "is_private",
        "is_deleted",
        "has_ean",
        "nutrients",
        "servings",
        "eans",
        "category",
        "language",
        "countries",
        "updated_at",
    }

    # Per one base unit, not per 100: olive oil reads 8.84 kcal per gram.
    nutrients = group_nutrients(detail.get("nutrients") or {})

    shaped: dict[str, Any] = {
        "product_id": detail.get("id"),
        "name": detail.get("name"),
        "producer": detail.get("producer"),
        "base_unit": detail.get("base_unit"),
        "is_verified": detail.get("is_verified"),
        # Says whether a product the user created stayed on their account or
        # went to YAZIO's shared database.
        "is_private": detail.get("is_private"),
        "category": detail.get("category"),
    }

    if brief:
        shaped["energy_kcal_per_base_unit"] = nutrients.get("energy_kcal")
        return {key: value for key, value in shaped.items() if value is not None}

    shaped["barcodes"] = detail.get("eans")
    shaped["servings"] = detail.get("servings")
    shaped["nutrients_per_base_unit"] = nutrients

    extra = {key: value for key, value in detail.items() if key not in known}
    if extra:
        shaped["extra"] = extra

    return {key: value for key, value in shaped.items() if value is not None}
