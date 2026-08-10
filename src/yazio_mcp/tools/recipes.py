"""Reading, creating, updating, photographing and favouriting recipes.

Creating and updating are the only tools in this server that are more than a
translation of an API call. YAZIO's recipe endpoints do not compute anything:
the client is expected to submit the recipe's nutrients alongside its
ingredients. The stored `nutrients` field holds the values **for one portion**,
not for the whole dish — that is what the app writes and what it reads back.
So create_recipe resolves every ingredient, scales its nutrients to the amount
used, sums them into the dish total and then divides by portion_count before
posting; update_recipe does the same when new ingredients are given, and
otherwise rescales what is already stored if the portion count changed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from io import BytesIO
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from yazio_sdk.api.content import list_featured_recipes as api_list_featured_recipes
from yazio_sdk.api.recipes import add_favorite_recipe as api_add_favorite_recipe
from yazio_sdk.api.recipes import add_user_recipe_image as api_add_user_recipe_image
from yazio_sdk.api.recipes import create_user_recipe as api_create_user_recipe
from yazio_sdk.api.recipes import delete_user_recipe as api_delete_user_recipe
from yazio_sdk.api.recipes import delete_user_recipe_image as api_delete_user_recipe_image
from yazio_sdk.api.recipes import get_recipe as api_get_recipe
from yazio_sdk.api.recipes import list_favorite_recipes as api_list_favorite_recipes
from yazio_sdk.api.recipes import list_user_recipes as api_list_user_recipes
from yazio_sdk.api.recipes import remove as api_remove_favorite
from yazio_sdk.api.recipes import update_user_recipe as api_update_user_recipe
from yazio_sdk.models import (
    AddUserRecipeImageBody,
    FavoriteRecipe,
    RecipeDraft,
    RecipeDraftNutrients,
    RecipeDraftServingsItem,
)
from yazio_sdk.types import File

from ..common import plain, round_floats
from ..nutrients import group_nutrients, scale_nutrients, sum_nutrients
from ..session import expect_ok, expect_written, yazio_client
from .products import fetch_product

# How many of the user's own recipes to expand per call. Listing returns bare
# ids, so each one costs a request; this keeps a "show my recipes" call from
# turning into a hundred round trips.
_MAX_EXPANDED = 25


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def list_my_recipes(
        ctx: Context, limit: int = _MAX_EXPANDED
    ) -> dict[str, Any]:
        """List the recipes this user has created or saved.

        Args:
            limit: How many recipes to load in full. The total count is always reported.
        """
        async with yazio_client(ctx) as client:
            response = await api_list_user_recipes.asyncio_detailed(client=client)
            recipe_ids = expect_ok(ctx, response, "list your recipes")

            wanted = recipe_ids[: max(0, min(limit, _MAX_EXPANDED))]
            recipes = await asyncio.gather(
                *(_load_recipe(ctx, client, recipe_id) for recipe_id in wanted)
            )

        return round_floats(
            {
                "total": len(recipe_ids),
                "returned": len(recipes),
                "recipes": [
                    _shape_recipe(recipe, brief=True) for recipe in recipes if recipe
                ],
            }
        )

    @mcp.tool()
    async def get_recipe(ctx: Context, recipe_id: str) -> dict[str, Any]:
        """Get one recipe in full: ingredients, instructions and nutrients.

        Nutrients are reported both for the whole dish and per portion.

        Args:
            recipe_id: The recipe's UUID.
        """
        async with yazio_client(ctx) as client:
            recipe = await _load_recipe(ctx, client, recipe_id)

        if recipe is None:
            raise ToolError(f"no recipe found with id {recipe_id}")

        return round_floats(_shape_recipe(recipe, brief=False))

    @mcp.tool()
    async def get_favorite_recipes(ctx: Context) -> dict[str, Any]:
        """List the recipes this user has marked as favourites.

        Each entry carries the `recipe_id` the other recipe tools take, and the
        portion count the recipe was favourited at.
        """
        async with yazio_client(ctx) as client:
            favorites = await _load_favorites(ctx, client)

        return round_floats(
            {"favorites": [_shape_favorite(favorite) for favorite in favorites]}
        )

    @mcp.tool()
    async def favorite_recipe(
        ctx: Context, recipe_id: str, portion_count: float | None = None
    ) -> dict[str, Any]:
        """Mark a recipe as one of this user's favourites.

        Any recipe can be favourited, the user's own and YAZIO's alike. A
        favourite remembers the portion count it was saved at, which is what the
        app offers when logging it later; favouriting a recipe that is already a
        favourite changes that count rather than listing the recipe twice.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes, get_recipe or
                browse_recipes.
            portion_count: How many portions to remember the recipe at. Defaults
                to the recipe's own portion count.
        """
        if portion_count is not None and portion_count <= 0:
            raise ToolError("portion_count must be greater than zero")

        async with yazio_client(ctx) as client:
            # Loading the recipe first turns an id that names nothing into an
            # explanation: the favourite endpoint answers 204 whatever it is
            # given, so a typo would otherwise be reported as a success.
            recipe = await _load_recipe(ctx, client, recipe_id)
            if recipe is None:
                raise ToolError(f"no recipe found with id {recipe_id}")

            existing = _favorite_ids_for(await _load_favorites(ctx, client), recipe_id)
            # A favourite has an id of its own — that is what
            # DELETE /v22/user/favorites/{id} removes — so minting a fresh one
            # for a recipe that is already favourited risks leaving the same
            # recipe in the list under two ids. Reusing the stored id makes a
            # repeat call an update of its portion count instead.
            favorite_id = existing[0] if existing else str(uuid.uuid4()).upper()
            portions = float(
                portion_count
                if portion_count is not None
                else (plain(recipe.portion_count) or 1)
            )

            response = await api_add_favorite_recipe.asyncio_detailed(
                client=client,
                body=FavoriteRecipe(
                    id=favorite_id, recipe_id=recipe_id, portion_count=portions
                ),
            )
            expect_written(ctx, response, f"favourite the recipe '{plain(recipe.name)}'")

        return round_floats(
            {
                "favorited": True,
                "recipe_id": recipe_id,
                "name": plain(recipe.name),
                "portion_count": portions,
                "favorite_id": favorite_id,
                "was_already_a_favorite": bool(existing),
            }
        )

    @mcp.tool()
    async def unfavorite_recipe(ctx: Context, recipe_id: str) -> dict[str, Any]:
        """Remove a recipe from this user's favourites.

        The recipe itself is untouched, as are any portions already logged
        against it — use delete_recipe to remove one of this user's own recipes.

        Args:
            recipe_id: The recipe's UUID, as reported by get_favorite_recipes.
        """
        async with yazio_client(ctx) as client:
            favorite_ids = _favorite_ids_for(await _load_favorites(ctx, client), recipe_id)
            if not favorite_ids:
                raise ToolError(
                    f"recipe {recipe_id} is not one of your favourites. This "
                    "takes the id of the recipe itself — check "
                    "get_favorite_recipes for the ones you have favourited."
                )

            # Removal is by the favourite's own id, not the recipe's, and
            # nothing stops a second entry pointing at the same recipe — one
            # written by another client before this tool existed, say. Each has
            # to go, or the recipe stays in the list.
            for favorite_id in favorite_ids:
                response = await api_remove_favorite.asyncio_detailed(
                    client=client, id=favorite_id
                )
                expect_written(
                    ctx, response, f"remove recipe {recipe_id} from your favourites"
                )

        return {
            "unfavorited": True,
            "recipe_id": recipe_id,
            "removed": len(favorite_ids),
        }

    @mcp.tool()
    async def browse_recipes(ctx: Context, country_code: str = "de") -> dict[str, Any]:
        """Browse YAZIO's own recipe catalogue for a country.

        This is the editorial collection shown in the app, not the user's own
        recipes. Use list_my_recipes for those.

        Args:
            country_code: Two-letter country code, such as "de" or "us".
        """
        async with yazio_client(ctx) as client:
            response = await api_list_featured_recipes.asyncio_detailed(
                client=client, country_code=country_code.lower()
            )
            catalogue = expect_ok(
                ctx, response, f"browse the {country_code} recipe catalogue"
            )

        return round_floats(
            {"country_code": country_code.lower(), "catalogue": plain(catalogue)}
        )

    @mcp.tool()
    async def create_recipe(
        ctx: Context,
        name: str,
        ingredients: list[dict[str, Any]],
        portion_count: int = 2,
        instructions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a recipe from a list of tracked products.

        Each ingredient must name a product from search_products and how much of
        it the recipe uses, either as an `amount` in the product's base unit
        (grams or millilitres) or as a `serving` plus `serving_quantity`:

            [{"product_id": "…", "amount": 250},
             {"product_id": "…", "serving": "can", "serving_quantity": 1}]

        At least two ingredients are required — YAZIO rejects a recipe with one.

        The recipe's nutrients are computed from the ingredients — YAZIO stores
        what the client submits and does not derive them — so every ingredient
        must resolve to a real product before the recipe can be created. What is
        stored is the nutrients of one portion, so the dish total is divided by
        portion_count on the way out.

        Args:
            name: Name of the recipe.
            ingredients: Products and quantities, as described above. Two or more.
            portion_count: How many portions the finished recipe makes. Whole
                portions only.
            instructions: Preparation steps, one string per step.
        """
        if not name.strip():
            raise ToolError("a recipe needs a name")
        # YAZIO rejects a one-ingredient recipe outright ("This collection should
        # contain 2 elements or more"). Catching it here explains why, rather
        # than surfacing a 400 from the middle of a submission.
        if len(ingredients) < 2:
            raise ToolError(
                "YAZIO requires at least two ingredients in a recipe; "
                f"got {len(ingredients)}. To log a single food, use track_product."
            )
        # A fractional portion_count is answered with a 500, not a validation
        # error, so it has to be caught here.
        if int(portion_count) != portion_count:
            raise ToolError(
                f"portion_count must be a whole number of portions; got {portion_count}"
            )
        portion_count = int(portion_count)
        if portion_count <= 0:
            raise ToolError("portion_count must be greater than zero")

        async with yazio_client(ctx) as client:
            resolved = await asyncio.gather(
                *(
                    _resolve_ingredient(client, item, index)
                    for index, item in enumerate(ingredients)
                )
            )

            total_nutrients = sum_nutrients([item["nutrients"] for item in resolved])
            # YAZIO's `nutrients` field is per portion, not per dish.
            per_portion = scale_nutrients(total_nutrients, 1 / float(portion_count))

            draft = RecipeDraft(
                id=str(uuid.uuid4()).upper(),
                name=name.strip(),
                # Must serialise as an integer: YAZIO answers 500 to a
                # fractional portion_count, and 2.0 counts as fractional.
                portion_count=portion_count,
                instructions=list(instructions or []),
                servings=[item["serving_entry"] for item in resolved],
                nutrients=RecipeDraftNutrients.from_dict(per_portion),
            )

            response = await api_create_user_recipe.asyncio_detailed(
                client=client, body=draft
            )
            expect_written(ctx, response, f"create the recipe '{name}'")

        return round_floats(
            {
                "created": True,
                "recipe_id": draft.id,
                "name": draft.name,
                "portion_count": portion_count,
                "ingredients": [
                    {
                        "product_id": item["serving_entry"].product_id,
                        "name": item["serving_entry"].name,
                        "amount": item["serving_entry"].amount,
                        "base_unit": item["serving_entry"].base_unit,
                        "serving": plain(item["serving_entry"].serving),
                        "serving_quantity": plain(
                            item["serving_entry"].serving_quantity
                        ),
                    }
                    for item in resolved
                ],
                "nutrients_total": group_nutrients(total_nutrients),
                "nutrients_per_portion": group_nutrients(per_portion),
            }
        )

    @mcp.tool()
    async def update_recipe(
        ctx: Context,
        recipe_id: str,
        name: str | None = None,
        ingredients: list[dict[str, Any]] | None = None,
        portion_count: int | None = None,
        instructions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update one of this user's own recipes.

        Only the arguments given are changed; anything omitted keeps its
        current value. `ingredients`, if given, replaces the whole ingredient
        list rather than patching it — see create_recipe for the expected
        format, and the same two-ingredient minimum applies.

        Changing `portion_count` alone keeps the dish the same size: the stored
        per-portion nutrients are rescaled so the total across all portions is
        unchanged.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes.
            name: New name for the recipe.
            ingredients: Full replacement ingredient list, as in create_recipe.
            portion_count: New portion count. Whole portions only.
            instructions: Full replacement preparation steps, one per step.
        """
        if name is not None and not name.strip():
            raise ToolError("a recipe needs a name")
        if ingredients is not None and len(ingredients) < 2:
            raise ToolError(
                "YAZIO requires at least two ingredients in a recipe; "
                f"got {len(ingredients)}. To log a single food, use track_product."
            )
        if portion_count is not None:
            # A fractional portion_count is answered with a 500, not a
            # validation error, so it has to be caught here.
            if int(portion_count) != portion_count:
                raise ToolError(
                    f"portion_count must be a whole number of portions; got {portion_count}"
                )
            portion_count = int(portion_count)
            if portion_count <= 0:
                raise ToolError("portion_count must be greater than zero")

        async with yazio_client(ctx) as client:
            # YAZIO's update endpoint answers a foreign recipe id the same way
            # it answers any other write, so this membership check is the only
            # thing that turns "not yours" into an explanation rather than a
            # bare status code.
            response = await api_list_user_recipes.asyncio_detailed(client=client)
            owned_ids = expect_ok(ctx, response, "list your recipes")
            if recipe_id not in owned_ids:
                raise ToolError(
                    f"recipe {recipe_id} is not one of your own recipes, so it "
                    "can't be updated. Only recipes you created can be edited "
                    "— check list_my_recipes for the ids you own."
                )

            recipe = await _load_recipe(ctx, client, recipe_id)
            if recipe is None:
                raise ToolError(f"no recipe found with id {recipe_id}")

            final_name = name.strip() if name is not None else (plain(recipe.name) or "")
            current_portion_count = int(plain(recipe.portion_count) or 1)
            final_portion_count = (
                portion_count if portion_count is not None else current_portion_count
            )
            final_instructions = (
                list(instructions)
                if instructions is not None
                else list(plain(recipe.instructions) or [])
            )

            if ingredients is not None:
                resolved = await asyncio.gather(
                    *(
                        _resolve_ingredient(client, item, index)
                        for index, item in enumerate(ingredients)
                    )
                )
                total_nutrients = sum_nutrients([item["nutrients"] for item in resolved])
                per_portion = scale_nutrients(
                    total_nutrients, 1 / float(final_portion_count)
                )
                servings = [item["serving_entry"] for item in resolved]
            else:
                # The stored field is per portion, for the portion count the
                # recipe had. Splitting the same dish differently has to rescale
                # it, or changing 2 portions to 4 would silently double the dish.
                stored = plain(recipe.nutrients) or {}
                per_portion = (
                    scale_nutrients(
                        stored, current_portion_count / float(final_portion_count)
                    )
                    if final_portion_count != current_portion_count
                    else dict(stored)
                )
                total_nutrients = scale_nutrients(
                    per_portion, float(final_portion_count)
                )
                servings = _draft_servings_from_recipe(recipe)

            draft = RecipeDraft(
                id=recipe_id,
                name=final_name,
                portion_count=final_portion_count,
                instructions=final_instructions,
                servings=servings,
                nutrients=RecipeDraftNutrients.from_dict(per_portion),
            )

            response = await api_update_user_recipe.asyncio_detailed(
                client=client, id=recipe_id, body=draft
            )
            expect_written(ctx, response, f"update the recipe '{final_name}'")

        return round_floats(
            {
                "updated": True,
                "recipe_id": recipe_id,
                "name": final_name,
                "portion_count": final_portion_count,
                "instructions": final_instructions,
                "nutrients_total": group_nutrients(total_nutrients),
                "nutrients_per_portion": group_nutrients(per_portion),
            }
        )

    @mcp.tool()
    async def delete_recipe(ctx: Context, recipe_id: str) -> dict[str, Any]:
        """Delete one of this user's own recipes.

        Only removes the recipe itself. Diary entries that already logged
        portions of it are untouched — use untrack_item to remove those.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes.
        """
        async with yazio_client(ctx) as client:
            recipe = await _load_recipe(ctx, client, recipe_id)
            if recipe is None:
                raise ToolError(f"no recipe found with id {recipe_id}")

            response = await api_delete_user_recipe.asyncio_detailed(
                client=client, id=recipe_id
            )
            expect_written(ctx, response, f"delete recipe {recipe_id}")

        return {
            "deleted": True,
            "recipe_id": recipe_id,
            "name": plain(recipe.name),
        }

    @mcp.tool()
    async def set_recipe_photo(
        ctx: Context, recipe_id: str, image_base64: str, extension: str = "jpg"
    ) -> dict[str, Any]:
        """Upload a photo for one of this user's own recipes.

        Replaces any photo the recipe already has.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes.
            image_base64: The photo's file contents, base64-encoded.
            extension: The photo's file extension, such as "jpg", "png" or "webp".
        """
        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(f"image_base64 is not valid base64: {exc}") from exc
        if not image_bytes:
            raise ToolError("image_base64 decoded to an empty file")

        async with yazio_client(ctx) as client:
            recipe = await _require_own_recipe(ctx, client, recipe_id)

            # filename is never read back by YAZIO — only its extension ends up
            # mattering — so a fresh uuid works just as well as the client's own,
            # matching how create_recipe mints the recipe id itself.
            filename = f"{uuid.uuid4()}.{extension.strip().lstrip('.').lower() or 'jpg'}"
            body = AddUserRecipeImageBody(image=File(payload=BytesIO(image_bytes)))
            response = await api_add_user_recipe_image.asyncio_detailed(
                client=client, id=recipe_id, filename=filename, body=body
            )
            expect_written(ctx, response, f"upload a photo for recipe {recipe_id}")

        return {
            "updated": True,
            "recipe_id": recipe_id,
            "name": plain(recipe.name),
        }

    @mcp.tool()
    async def delete_recipe_photo(ctx: Context, recipe_id: str) -> dict[str, Any]:
        """Remove the photo from one of this user's own recipes.

        Args:
            recipe_id: The recipe's UUID, from list_my_recipes.
        """
        async with yazio_client(ctx) as client:
            recipe = await _require_own_recipe(ctx, client, recipe_id)

            response = await api_delete_user_recipe_image.asyncio_detailed(
                client=client, id=recipe_id
            )
            expect_written(ctx, response, f"delete the photo for recipe {recipe_id}")

        return {
            "deleted": True,
            "recipe_id": recipe_id,
            "name": plain(recipe.name),
        }


async def _load_recipe(ctx: Context, client: Any, recipe_id: str) -> Any:
    response = await api_get_recipe.asyncio_detailed(client=client, id=recipe_id)
    if response.status_code == 404:
        return None
    return expect_ok(ctx, response, f"load recipe {recipe_id}")


async def _load_favorites(ctx: Context, client: Any) -> list[Any]:
    response = await api_list_favorite_recipes.asyncio_detailed(client=client)
    return expect_ok(ctx, response, "load your favourite recipes")


def _favorite_ids_for(favorites: list[Any], recipe_id: str) -> list[str]:
    """The ids of the favourite entries pointing at one recipe.

    Both writes are addressed by the favourite's own id while a caller only ever
    has the recipe's, so every favourite tool starts by translating one into the
    other.
    """
    return [
        str(plain(favorite.id))
        for favorite in favorites
        if plain(favorite.recipe_id) == recipe_id and plain(favorite.id)
    ]


def _shape_favorite(favorite: Any) -> dict[str, Any]:
    """Present one favourite entry, keeping its two ids apart.

    The payload calls the favourite's own id `id`, which reads like the
    recipe's; passing it to get_recipe or track_recipe finds nothing. Naming it
    `favorite_id` leaves `recipe_id` as the one id the other tools take.
    """
    return {
        "recipe_id": plain(favorite.recipe_id),
        "portion_count": plain(favorite.portion_count),
        "favorite_id": plain(favorite.id),
        "yazio_id": plain(favorite.yazio_id),
    }


async def _require_own_recipe(ctx: Context, client: Any, recipe_id: str) -> Any:
    """Load a recipe, refusing one that isn't this user's own.

    YAZIO's photo endpoints answer a foreign recipe id the same way they answer
    any other write, so this membership check is the only thing that turns
    "not yours" into an explanation rather than a bare status code.
    """
    response = await api_list_user_recipes.asyncio_detailed(client=client)
    owned_ids = expect_ok(ctx, response, "list your recipes")
    if recipe_id not in owned_ids:
        raise ToolError(
            f"recipe {recipe_id} is not one of your own recipes, so its photo "
            "can't be changed. Only recipes you created can be edited — check "
            "list_my_recipes for the ids you own."
        )

    recipe = await _load_recipe(ctx, client, recipe_id)
    if recipe is None:
        raise ToolError(f"no recipe found with id {recipe_id}")
    return recipe


def _shape_recipe(recipe: Any, brief: bool) -> dict[str, Any]:
    """Present a recipe, optionally without its ingredient list.

    The brief form is used when listing, where a dozen full ingredient lists
    would bury the names the caller is actually choosing between.

    The stored `nutrients` field is already per portion, so the whole dish is
    that multiplied by portion_count.
    """
    portion_count = plain(recipe.portion_count) or 1
    nutrients = plain(recipe.nutrients) or {}

    shaped: dict[str, Any] = {
        "recipe_id": plain(recipe.id),
        "name": plain(recipe.name),
        "portion_count": portion_count,
        "is_yazio_recipe": plain(recipe.is_yazio_recipe),
        "is_pro_recipe": plain(recipe.is_pro_recipe),
        "nutrients_per_portion": group_nutrients(nutrients),
    }

    if not brief:
        shaped["nutrients_total"] = group_nutrients(
            scale_nutrients(nutrients, float(portion_count))
        )
        shaped["servings"] = plain(recipe.servings)
        shaped["instructions"] = plain(recipe.instructions)
        shaped["locale"] = plain(recipe.locale)
        shaped["image"] = plain(recipe.image)

    return shaped


async def _resolve_ingredient(
    client: Any, item: dict[str, Any], index: int
) -> dict[str, Any]:
    """Turn one ingredient spec into a serving entry plus its scaled nutrients."""
    if not isinstance(item, dict):
        raise ToolError(f"ingredient {index + 1} must be an object with a product_id")

    product_id = item.get("product_id")
    if not product_id:
        raise ToolError(f"ingredient {index + 1} is missing product_id")

    product = await fetch_product(client, str(product_id))
    if product is None:
        raise ToolError(
            f"ingredient {index + 1}: no product found with id {product_id}"
        )

    serving = item.get("serving")
    quantity = item.get("serving_quantity")
    amount = item.get("amount")

    if amount is None and serving is None:
        raise ToolError(
            f"ingredient {index + 1} ({product.get('name')}) needs either an amount "
            "or a serving with serving_quantity"
        )
    # An amount of zero or less is nonsense in a recipe and the API takes it
    # without complaint, so it has to be refused here — as portion_count is.
    if amount is not None and float(amount) <= 0:
        raise ToolError(
            f"ingredient {index + 1} ({product.get('name')}): amount must be "
            f"greater than zero; got {amount}"
        )

    # Reuse the tracking rules so a recipe ingredient and a tracked portion mean
    # the same thing for the same numbers.
    from .tracking import _resolve_amount

    resolved_amount = _resolve_amount(product, amount, serving, quantity)

    serving_entry = RecipeDraftServingsItem(
        product_id=str(product_id),
        name=product.get("name") or "",
        producer=product.get("producer") or "",
        base_unit=product.get("base_unit") or "g",
        amount=resolved_amount,
    )
    # Only label the entry with a serving when the serving is what produced the
    # amount. An explicit `amount` overrides the serving in _resolve_amount, so
    # carrying the label over would store 250 ml of nutrients as "1 can".
    if serving is not None and amount is None:
        serving_entry.serving = str(serving)
        serving_entry.serving_quantity = float(quantity if quantity is not None else 1)

    return {
        "serving_entry": serving_entry,
        "nutrients": _nutrients_for_amount(product, resolved_amount),
    }


def _draft_servings_from_recipe(recipe: Any) -> list[RecipeDraftServingsItem]:
    """Rebuild a draft's servings list from an already-loaded recipe.

    update_recipe uses this when called without new ingredients: the update
    endpoint takes a full RecipeDraft, not a patch, so the existing servings
    have to be resent unchanged alongside whatever did change.
    """
    servings = plain(recipe.servings) or []
    entries = []
    for item in servings:
        entry = RecipeDraftServingsItem(
            product_id=item.get("product_id") or "",
            name=item.get("name") or "",
            producer=item.get("producer") or "",
            base_unit=item.get("base_unit") or "g",
            amount=item.get("amount") or 0,
        )
        if item.get("serving") is not None:
            entry.serving = str(item["serving"])
        if item.get("serving_quantity") is not None:
            entry.serving_quantity = float(item["serving_quantity"])
        entries.append(entry)
    return entries


def _nutrients_for_amount(product: dict[str, Any], amount: float) -> dict[str, float]:
    """Scale a product's nutrient table to the amount a recipe uses.

    YAZIO stores product nutrients per single base unit — per gram, not per
    100 g — so the scale factor is the amount itself. The product payload is
    checked for an explicit basis first in case that ever stops being true.
    """
    nutrients = product.get("nutrients")
    if not isinstance(nutrients, dict) or not nutrients:
        raise ToolError(
            f"product {product.get('name', '')} has no nutrient data, "
            "so it cannot be used as a recipe ingredient"
        )

    basis = product.get("nutrients_base_amount") or product.get("base_amount") or 1.0
    return scale_nutrients(nutrients, amount / float(basis))
