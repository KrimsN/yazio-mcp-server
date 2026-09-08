"""Reshapes YAZIO's flat, dotted nutrient maps into grouped JSON.

The API returns nutrients as a single flat map with keys like `energy.energy`,
`nutrient.protein`, `mineral.calcium` and `vitamin.b12`. A full product carries
around forty of these. Nothing is dropped here — the grouping only makes the
shape self-describing, so a caller can tell at a glance which numbers are macros
and which are micronutrients, without having to know YAZIO's key vocabulary.

YAZIO stores energy in kilocalories and every other nutrient in grams. Grams are
kept for macros, but minerals and vitamins are converted to milligrams on the way
out: in grams they are values like 0.00012, small enough that any downstream
rounding reports them as zero. Each group's unit is stated in the output so the
mixture cannot be misread.

`flatten_nutrients` runs the same translation backwards, for the one place a
caller supplies nutrients rather than reads them: creating a product. It takes
the units this module reports in and returns the flat dotted map the API stores.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

# YAZIO's own ordering for the four headline numbers. Anything outside this list
# still comes through, just after these.
_MACRO_ORDER = ("carb", "protein", "fat")

_GROUPS = {
    "nutrient": "macros",
    "mineral": "minerals",
    "vitamin": "vitamins",
}

# What each group is reported in, and the factor from YAZIO's stored grams.
# Milligrams for the micronutrient families keeps them in the range a label
# would print; `other` holds keys we do not recognise, so it is left in the
# unit YAZIO stored.
_UNITS = {
    "macros": ("g", 1.0),
    "minerals": ("mg", 1000.0),
    "vitamins": ("mg", 1000.0),
    "other": ("g", 1.0),
}

# The names a caller may use for a nutrient without knowing YAZIO's vocabulary.
# Only the four the app itself puts on a label are aliased: the rest of YAZIO's
# forty-odd keys are undocumented, and guessing `nutrient.fiber` for whatever the
# API actually calls fibre would store a number that nothing ever reads back.
# Everything else therefore has to be named by its exact dotted key.
_ALIASES = {
    "calories": "energy.energy",
    "carb": "nutrient.carb",
    "carbohydrate": "nutrient.carb",
    "carbohydrates": "nutrient.carb",
    "carbs": "nutrient.carb",
    "energy": "energy.energy",
    "energy_kcal": "energy.energy",
    "fat": "nutrient.fat",
    "kcal": "energy.energy",
    "protein": "nutrient.protein",
}

# What a caller's number has to be multiplied by to reach YAZIO's own storage
# unit — the inverse of the conversion `group_nutrients` applies on the way out,
# so a value read from one product can be handed straight to another.
_INPUT_FACTORS = {
    "energy": 1.0,
    "nutrient": 1.0,
    "mineral": 0.001,
    "vitamin": 0.001,
}

# The keys a caller is expected to fill in, named here so that the rules about
# them live with the vocabulary rather than being spelled out again elsewhere.
ENERGY_KEY = "energy.energy"
MACRO_KEYS = tuple(f"nutrient.{name}" for name in _MACRO_ORDER)

# Every dotted key YAZIO's own client is known to send, beyond the four
# aliased above. Sourced from yazio-api-specification's Recipe schema, where
# the full set is required rather than optional — a caller can look up a key
# such as salt or saturated fat here instead of guessing wrong against the
# live API. Not exhaustive: `_api_key` accepts any key of this shape, and new
# families turn up as more traffic gets captured.
_KNOWN_KEYS = {
    "nutrient": (
        "nutrient.alcohol",
        "nutrient.carb",
        "nutrient.cholesterol",
        "nutrient.dietaryfiber",
        "nutrient.fat",
        "nutrient.monounsaturated",
        "nutrient.polyunsaturated",
        "nutrient.protein",
        "nutrient.salt",
        "nutrient.saturated",
        "nutrient.sodium",
        "nutrient.sugar",
        "nutrient.water",
    ),
    "mineral": (
        "mineral.arsenic",
        "mineral.boron",
        "mineral.calcium",
        "mineral.chlorine",
        "mineral.chrome",
        "mineral.copper",
        "mineral.fluoride",
        "mineral.fluorine",
        "mineral.iodine",
        "mineral.iron",
        "mineral.magnesium",
        "mineral.manganese",
        "mineral.phosphorus",
        "mineral.potassium",
        "mineral.selenium",
        "mineral.sulfur",
        "mineral.zinc",
    ),
    "vitamin": (
        "vitamin.a",
        "vitamin.b1",
        "vitamin.b11",
        "vitamin.b12",
        "vitamin.b2",
        "vitamin.b3",
        "vitamin.b5",
        "vitamin.b6",
        "vitamin.b7",
        "vitamin.c",
        "vitamin.d",
        "vitamin.e",
        "vitamin.k",
    ),
}

# Short glosses for keys whose YAZIO name does not say what they are. Most
# keys need none — `mineral.calcium` is calcium — so only the handful that
# would otherwise send a caller guessing are listed here.
_GLOSSES = {
    "mineral.chrome": "chromium, the mineral — not a display or browser setting",
    "vitamin.b11": "folate / folic acid, not a mainstream numbering of B vitamins",
    "nutrient.saturated": "the saturated share of nutrient.fat, not an addition to it",
    "nutrient.monounsaturated": "the monounsaturated share of nutrient.fat, not an addition to it",
    "nutrient.polyunsaturated": "the polyunsaturated share of nutrient.fat, not an addition to it",
    "nutrient.dietaryfiber": "dietary fibre",
}


def nutrient_reference() -> dict[str, Any]:
    """Build the reference payload served by the `yazio://nutrients` resource.

    Lets a caller look up a nutrient's exact dotted key before calling
    create_product or create_recipe, rather than only after a ToolError — or
    worse, after the API has silently stored a misspelt one.
    """
    return {
        "aliases": dict(sorted(_ALIASES.items())),
        "known_keys": {family: list(keys) for family, keys in _KNOWN_KEYS.items()},
        "glosses": dict(sorted(_GLOSSES.items())),
        "units": {
            "energy.energy": "kcal",
            "nutrient.*": "g",
            "mineral.*": "mg",
            "vitamin.*": "mg",
        },
        "note": (
            "Values are supplied per one base unit, in these units — same as "
            "get_product reports them. A key outside this list is still accepted "
            "if it follows the '<family>.<name>' shape with family one of energy, "
            "nutrient, mineral, vitamin; this list is what YAZIO's own client is "
            "known to send, not an exhaustive schema."
        ),
    }


def group_nutrients(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Turn a flat dotted nutrient map into grouped, unit-annotated JSON.

    Keys that do not follow the `prefix.name` convention, or that use a prefix
    we do not recognise, are preserved verbatim under `other` rather than being
    silently discarded — the spec was inferred from traffic, so new nutrient
    families are expected to turn up.
    """
    if not raw:
        return {}

    grouped: dict[str, dict[str, float]] = {
        "macros": {},
        "minerals": {},
        "vitamins": {},
        "other": {},
    }
    energy_kcal: float | None = None

    for key, value in raw.items():
        if not isinstance(value, (int, float)):
            grouped["other"][key] = value
            continue

        prefix, _, name = key.partition(".")
        if key == "energy.energy":
            energy_kcal = float(value)
        elif name and prefix in _GROUPS:
            grouped[_GROUPS[prefix]][name] = float(value)
        else:
            grouped["other"][key] = float(value)

    units: dict[str, str] = {}
    result: dict[str, Any] = {"units": units}
    if energy_kcal is not None:
        units["energy"] = "kcal"
        result["energy_kcal"] = energy_kcal

    if grouped["macros"]:
        units["macros"] = _UNITS["macros"][0]
        result["macros"] = _ordered(
            _converted(grouped["macros"], "macros"), _MACRO_ORDER
        )
    for group in ("minerals", "vitamins", "other"):
        if grouped[group]:
            units[group] = _UNITS[group][0]
            values = _converted(grouped[group], group)
            result[group] = dict(sorted(values.items()))

    return result


def _converted(values: dict[str, Any], group: str) -> dict[str, Any]:
    """Restate one group's values in that group's reporting unit."""
    factor = _UNITS[group][1]
    if factor == 1.0:
        return values
    return {
        key: value * factor if isinstance(value, float) else value
        for key, value in values.items()
    }


def _ordered(values: dict[str, float], first: tuple[str, ...]) -> dict[str, float]:
    """Sort a group so that well-known keys lead and the rest follow by name."""
    leading = {key: values[key] for key in first if key in values}
    trailing = {
        key: value for key, value in sorted(values.items()) if key not in leading
    }
    return {**leading, **trailing}


def flatten_nutrients(values: Mapping[str, Any], basis: float) -> dict[str, float]:
    """Turn a caller's nutrient table into the flat, per-base-unit map YAZIO stores.

    The inverse of `group_nutrients`, and it reads the same units: energy in
    kilocalories, macros in grams, minerals and vitamins in milligrams. `basis`
    is how many base units the numbers describe — a label reading "per 100 g" is
    a basis of 100 — because a stored product is always per one.

    Names outside the alias table have to be exact dotted keys, which is a
    deliberate refusal to guess: the API takes any key at all and stores it
    without complaint, so a misspelt nutrient would look accepted and then read
    back as absent for the life of the product.
    """
    if basis <= 0:
        raise ToolError(f"the nutrient basis must be greater than zero; got {basis}")
    if not values:
        raise ToolError("a product needs its nutrients; none were given")

    flat: dict[str, float] = {}
    for key, value in values.items():
        api_key = _api_key(key)
        if api_key in flat:
            raise ToolError(
                f"'{key}' names {api_key}, which was already given; "
                "each nutrient may only be set once"
            )
        family = api_key.partition(".")[0]
        flat[api_key] = _as_number(key, value) * _INPUT_FACTORS[family] / basis

    return flat


def _api_key(key: str) -> str:
    """Resolve one caller-supplied nutrient name to the key YAZIO stores it under."""
    name = str(key).strip().lower()
    if name in _ALIASES:
        return _ALIASES[name]

    prefix, dot, rest = name.partition(".")
    if dot and rest and prefix in _INPUT_FACTORS:
        return name

    raise ToolError(
        f"'{key}' is not a nutrient this server will store. Use one of "
        f"{', '.join(sorted(_ALIASES))}, the exact dotted key a product "
        "already carries in get_product, or one from the yazio://nutrients "
        "resource, such as 'mineral.calcium' or 'nutrient.salt'."
    )


def _as_number(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"nutrient '{key}' must be a number; got {value!r}")
    if value < 0:
        raise ToolError(f"nutrient '{key}' cannot be negative; got {value}")
    return float(value)


def scale_nutrients(raw: Mapping[str, Any] | None, factor: float) -> dict[str, float]:
    """Multiply every numeric nutrient by `factor`, keeping the flat dotted keys.

    Used when building a recipe: YAZIO expects the client to submit the summed
    nutrients of the finished dish, so each ingredient's per-serving values have
    to be scaled to the amount actually used and then added up.
    """
    if not raw:
        return {}

    return {
        key: float(value) * factor
        for key, value in raw.items()
        if isinstance(value, (int, float))
    }


def sum_nutrients(parts: list[Mapping[str, float]]) -> dict[str, float]:
    """Add up several flat nutrient maps, keeping every key that appears in any."""
    total: dict[str, float] = {}
    for part in parts:
        for key, value in part.items():
            total[key] = total.get(key, 0.0) + float(value)
    return total
