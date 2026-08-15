"""Writing to the diary: logging food, water and exercise.

Every tool here changes the user's data. They are deliberately narrow — one
action each, with the resolved result echoed back — so that a caller can tell
exactly what was written without re-reading the diary.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from io import BytesIO
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.api.diary import add_consumed_items as api_add_consumed_items
from yazio_sdk.api.diary import delete_consumed_item as api_delete_consumed_item
from yazio_sdk.api.diary import get_nutrimind_search_image as api_get_nutrimind_search_image
from yazio_sdk.api.diary import list_consumed_items as api_list_consumed_items
from yazio_sdk.api.diary import set_water_intake as api_set_water_intake
from yazio_sdk.api.diary import (
    upload_nutrimind_search_image as api_upload_nutrimind_search_image,
)
from yazio_sdk.api.user import get_user as api_get_user
from yazio_sdk.models import (
    ConsumedItems,
    ConsumedItemsDeletion,
    ConsumedItemsProductsItem,
    ConsumedItemsSimpleProductsItem,
    ConsumedRecipePortion,
    NutrientSummary,
    UploadNutrimindSearchImageBody,
    WaterIntakeEntry,
)
from yazio_sdk.types import File

from ..common import (
    plain,
    resolve_date,
    resolve_daytime,
    resolve_timestamp,
    round_floats,
)
from ..nutrients import group_nutrients
from ..session import expect_ok, expect_written, yazio_client
from .products import _search_country, _search_locales, fetch_product


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def track_product(
        ctx: Context,
        product_id: str,
        daytime: str,
        amount: float | None = None,
        serving: str | None = None,
        serving_quantity: float | None = None,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Log a product to the diary.

        Give the portion either as a raw `amount` in the product's base unit
        (grams or millilitres), or as a named `serving` plus how many of them —
        for example serving="can", serving_quantity=1. When a serving is named,
        the base-unit amount is looked up from the product so YAZIO records both
        consistently.

        Args:
            product_id: The product's UUID, from search_products.
            daytime: One of breakfast, lunch, dinner, snack.
            amount: Portion size in the product's base unit. Optional if serving is given.
            serving: Named serving such as "can", "slice", "gram".
            serving_quantity: How many of that serving. Defaults to 1 when serving is given.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        slot = resolve_daytime(daytime)
        day = resolve_date(date)

        if amount is None and serving is None:
            raise ToolError("give either an amount, or a serving to log a portion of")

        async with yazio_client(ctx) as client:
            product = await fetch_product(client, product_id)
            if product is None:
                raise ToolError(f"no product found with id {product_id}")

            quantity = (
                serving_quantity
                if serving_quantity is not None
                else (1.0 if serving else None)
            )
            resolved_amount = _resolve_amount(product, amount, serving, quantity)

            entry = ConsumedItemsProductsItem(
                id=str(uuid.uuid4()).upper(),
                product_id=product_id,
                amount=resolved_amount,
                daytime=slot,
                date=resolve_timestamp(date),
                **_serving_fields(serving, quantity),
            )

            response = await api_add_consumed_items.asyncio_detailed(
                client=client, body=ConsumedItems(products=[entry])
            )
            expect_written(ctx, response, f"log {product.get('name', product_id)}")

        return round_floats(
            {
                "tracked": True,
                "entry_id": entry.id,
                "date": day,
                "daytime": slot,
                "product_id": product_id,
                "name": product.get("name"),
                "amount": resolved_amount,
                "base_unit": product.get("base_unit"),
                "serving": serving,
                "serving_quantity": quantity,
            }
        )

    @mcp.tool()
    async def track_recipe(
        ctx: Context,
        recipe_id: str,
        daytime: str,
        portions: float = 1,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Log portions of a recipe to the diary.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes or get_recipe.
            daytime: One of breakfast, lunch, dinner, snack.
            portions: How many portions of the recipe were eaten.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        slot = resolve_daytime(daytime)
        day = resolve_date(date)

        if portions <= 0:
            raise ToolError("portions must be greater than zero")

        entry_id = str(uuid.uuid4()).upper()
        portion = ConsumedRecipePortion(
            id=entry_id,
            recipe_id=recipe_id,
            portion_count=float(portions),
            daytime=slot,
            date=resolve_timestamp(date),
        )

        async with yazio_client(ctx) as client:
            response = await api_add_consumed_items.asyncio_detailed(
                client=client, body=ConsumedItems(recipe_portions=[portion])
            )
            expect_written(ctx, response, f"log {portions} portion(s) of a recipe")

        return round_floats(
            {
                "tracked": True,
                "entry_id": entry_id,
                "date": day,
                "daytime": slot,
                "recipe_id": recipe_id,
                "portions": portions,
            }
        )

    @mcp.tool()
    async def track_meal_photo(
        ctx: Context,
        image_base64: str,
        daytime: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Log a meal from a photo, using YAZIO's AI food recognition.

        Uploads the photo and logs whatever the recognition model comes back
        with. That is always a single best guess rather than a list of
        candidates to choose between — the same as what the app's own camera
        flow shows before a tap on "confirm" — so review the name and
        nutrients in the result. If the guess is wrong, untrack_item removes
        the entry; search_products and track_product log the right food in
        its place.

        Args:
            image_base64: The meal photo's file contents, base64-encoded.
            daytime: One of breakfast, lunch, dinner, snack.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(f"image_base64 is not valid base64: {exc}") from exc
        if not image_bytes:
            raise ToolError("image_base64 decoded to an empty file")

        slot = resolve_daytime(daytime)
        day = resolve_date(date)
        search_id = str(uuid.uuid4()).upper()

        async with yazio_client(ctx) as client:
            # Recognition is ranked the same way product search is, and takes
            # the same two parameters — reusing the profile lookup keeps a
            # photo taken abroad matched against the right food database.
            profile_response = await api_get_user.asyncio_detailed(client=client)
            profile = expect_ok(ctx, profile_response, "read your search region")
            countries = _search_country(profile)
            locales = _search_locales(profile)

            upload_response = await api_upload_nutrimind_search_image.asyncio_detailed(
                id=search_id,
                client=client,
                body=UploadNutrimindSearchImageBody(
                    image=File(payload=BytesIO(image_bytes))
                ),
                countries=countries,
                locales=locales,
            )
            expect_written(ctx, upload_response, "upload the meal photo")

            result_response = await api_get_nutrimind_search_image.asyncio_detailed(
                id=search_id, client=client, countries=countries, locales=locales
            )
            result = expect_ok(ctx, result_response, "read the recognition result")

            # Every capture came back with one guess in simple_products and
            # nothing in products or ingredients — never a list to pick from.
            # The first entry is taken on the assumption that stays true; there
            # is no candidate-selection endpoint to fall back on if it changes.
            guesses = plain(result.simple_products) or []
            if not guesses:
                raise ToolError(
                    "YAZIO's recognition did not return a guess for this photo. "
                    "Try a clearer or closer photo, or log the food directly "
                    "with search_products and track_product."
                )
            guess = guesses[0]
            name = guess.get("name") or "Recognized meal"
            nutrients_raw = guess.get("nutrients") or {}

            entry_id = str(uuid.uuid4()).upper()
            entry = ConsumedItemsSimpleProductsItem(
                id=entry_id,
                date=resolve_timestamp(date),
                daytime=slot,
                name=name,
                nutrients=NutrientSummary.from_dict(nutrients_raw),
                is_ai_generated=True,
            )

            response = await api_add_consumed_items.asyncio_detailed(
                client=client, body=ConsumedItems(simple_products=[entry])
            )
            expect_written(ctx, response, f"log the recognized meal '{name}'")

        return round_floats(
            {
                "tracked": True,
                "entry_id": entry_id,
                "date": day,
                "daytime": slot,
                "name": name,
                "nutrients": group_nutrients(nutrients_raw),
                "is_ai_generated": True,
            }
        )

    @mcp.tool()
    async def untrack_item(
        ctx: Context,
        entry_id: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Remove a logged item from the diary.

        Takes the entry_id that get_diary returns for each item — not the
        product_id, which identifies the food rather than this particular
        logging of it.

        Args:
            entry_id: The diary entry's UUID, from get_diary.
            date: The day the entry is on, as YYYY-MM-DD. Defaults to today.
                Only needed to look the entry up; give it when removing
                something logged on another day.
        """
        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            kind = await _classify_entry(ctx, client, entry_id, day)

            response = await api_delete_consumed_item.asyncio_detailed(
                client=client, body=ConsumedItemsDeletion(**{kind: entry_id})
            )
            expect_written(ctx, response, f"remove diary entry {entry_id}")

            # The endpoint answers 204 whether or not it removed anything, so
            # the only way to report honestly is to look again.
            still_there = await _classify_entry(
                ctx, client, entry_id, day, missing_ok=True
            )

        if still_there is not None:
            raise ToolError(
                f"YAZIO accepted the removal of {entry_id} but the entry is still "
                f"on {day}. It may need to be removed in the app."
            )

        return {"removed": True, "entry_id": entry_id, "date": day, "kind": kind}

    @mcp.tool()
    async def log_water(
        ctx: Context,
        millilitres: float,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Set the day's water intake, in millilitres.

        This replaces the day's total rather than adding to it, matching how the
        app's water tracker works. Read get_water first if you mean to add to
        what is already logged.

        Args:
            millilitres: Total water for the day, in ml.
            date: Day to log against as YYYY-MM-DD. Defaults to today.
        """
        if millilitres < 0:
            raise ToolError("water intake cannot be negative")

        day = resolve_date(date)

        async with yazio_client(ctx) as client:
            response = await api_set_water_intake.asyncio_detailed(
                client=client,
                body=[
                    WaterIntakeEntry(
                        date=resolve_timestamp(date), water_intake=millilitres
                    )
                ],
            )
            expect_written(ctx, response, f"log water for {day}")

        return {"logged": True, "date": day, "water_intake_ml": millilitres}


async def _classify_entry(
    ctx: Context,
    client: Any,
    entry_id: str,
    day: str,
    missing_ok: bool = False,
) -> str | None:
    """Find which of the diary's three buckets an entry lives in.

    The deletion request names the bucket — `products`, `recipe_portions` or
    `simple_products` — and sending the wrong one is accepted with a 204 that
    removes nothing, so the entry has to be located first.
    """
    response = await api_list_consumed_items.asyncio_detailed(
        client=client, date=day
    )
    diary = expect_ok(ctx, response, f"find diary entry {entry_id}")

    wanted = entry_id.lower()
    for bucket in ("products", "recipe_portions", "simple_products"):
        for item in plain(getattr(diary, bucket)) or []:
            if isinstance(item, dict) and str(item.get("id", "")).lower() == wanted:
                return bucket

    if missing_ok:
        return None

    raise ToolError(
        f"no diary entry with id {entry_id} on {day}. "
        "Use get_diary to list the entries for that day, and pass its date if "
        "the entry is not on today."
    )


def _serving_fields(serving: str | None, quantity: float | None) -> dict[str, Any]:
    """Build the optional serving half of a consumed-item payload.

    Omitted entirely when logging a raw amount, because sending a null serving
    makes YAZIO show the entry without a portion label.
    """
    if serving is None:
        return {}
    return {
        "serving": serving,
        "serving_quantity": quantity if quantity is not None else 1.0,
    }


def _resolve_amount(
    product: dict[str, Any],
    amount: float | None,
    serving: str | None,
    quantity: float | None,
) -> float:
    """Work out the base-unit amount for a portion.

    An explicit amount always wins. Otherwise the named serving is looked up in
    the product's serving table and multiplied out. If the product does not list
    that serving we refuse rather than guess, because silently logging the wrong
    quantity is worse than asking the caller to pick a serving the product has.
    """
    if amount is not None:
        return float(amount)

    servings = product.get("servings")
    if not isinstance(servings, list):
        raise ToolError(
            f"product {product.get('name', '')} lists no servings; "
            "give an explicit amount in its base unit instead"
        )

    for option in servings:
        if not isinstance(option, dict):
            continue
        if option.get("serving") == serving or option.get("name") == serving:
            base_amount = option.get("amount")
            if isinstance(base_amount, (int, float)):
                return float(base_amount) * float(
                    quantity if quantity is not None else 1.0
                )

    available = sorted(
        str(option.get("serving") or option.get("name"))
        for option in servings
        if isinstance(option, dict) and (option.get("serving") or option.get("name"))
    )
    raise ToolError(
        f"product does not offer a '{serving}' serving. Available: {', '.join(available) or 'none'}"
    )
