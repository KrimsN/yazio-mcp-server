"""Reshaping and scaling YAZIO's dotted nutrient maps."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yazio_mcp.nutrients import (
    flatten_nutrients,
    group_nutrients,
    scale_nutrients,
    sum_nutrients,
)

SAMPLE = {
    "energy.energy": 884.0,
    "nutrient.fat": 100.0,
    "nutrient.carb": 0.0,
    "nutrient.protein": 0.0,
    "mineral.calcium": 0.001,
    "vitamin.e": 0.0142,
    "unknown.thing": 3.0,
}


def test_groups_by_family():
    grouped = group_nutrients(SAMPLE)

    assert grouped["energy_kcal"] == 884.0
    assert grouped["macros"] == {"carb": 0.0, "protein": 0.0, "fat": 100.0}
    assert grouped["minerals"] == pytest.approx({"calcium": 1.0})
    assert grouped["vitamins"] == pytest.approx({"e": 14.2})


def test_micronutrients_are_reported_in_milligrams():
    """In grams they are small enough that rounding would report them as zero."""
    grouped = group_nutrients({"mineral.calcium": 5e-05, "vitamin.c": 0.00012})

    assert grouped["minerals"]["calcium"] == pytest.approx(0.05)
    assert grouped["vitamins"]["c"] == pytest.approx(0.12)


def test_states_its_units():
    assert group_nutrients(SAMPLE)["units"] == {
        "energy": "kcal",
        "macros": "g",
        "minerals": "mg",
        "vitamins": "mg",
        "other": "g",
    }


def test_units_name_only_the_groups_that_are_present():
    assert group_nutrients({"mineral.calcium": 0.001})["units"] == {"minerals": "mg"}


def test_keeps_unrecognised_keys():
    """The spec is inferred from traffic, so unknown families must not vanish."""
    assert group_nutrients(SAMPLE)["other"]["unknown.thing"] == 3.0


def test_macros_lead_in_yazio_order():
    assert list(group_nutrients(SAMPLE)["macros"]) == ["carb", "protein", "fat"]


def test_loses_nothing():
    grouped = group_nutrients(SAMPLE)
    counted = (
        1  # energy
        + len(grouped["macros"])
        + len(grouped["minerals"])
        + len(grouped["vitamins"])
        + len(grouped["other"])
    )
    assert counted == len(SAMPLE)


def test_empty_input_yields_empty_output():
    assert group_nutrients({}) == {}
    assert group_nutrients(None) == {}


def test_omits_groups_that_have_no_values():
    assert "vitamins" not in group_nutrients({"energy.energy": 10.0})


def test_non_numeric_values_go_to_other():
    grouped = group_nutrients({"nutrient.note": "unknown"})
    assert grouped["other"] == {"nutrient.note": "unknown"}


def test_scaling_multiplies_every_value():
    scaled = scale_nutrients({"energy.energy": 9.0, "nutrient.fat": 1.0}, 250)
    assert scaled == {"energy.energy": 2250.0, "nutrient.fat": 250.0}


def test_scaling_keeps_the_dotted_keys():
    """The scaled map is posted back to YAZIO, so its keys must stay verbatim."""
    assert set(scale_nutrients(SAMPLE, 2)) == set(SAMPLE) - {"unknown.thing"} | {"unknown.thing"}


def test_summing_unions_keys():
    total = sum_nutrients(
        [
            {"energy.energy": 100.0, "nutrient.fat": 5.0},
            {"energy.energy": 50.0, "nutrient.protein": 2.0},
        ]
    )
    assert total == {
        "energy.energy": 150.0,
        "nutrient.fat": 5.0,
        "nutrient.protein": 2.0,
    }


def test_summing_nothing_is_empty():
    assert sum_nutrients([]) == {}


def test_flattening_divides_by_the_stated_basis():
    """A label states per 100 g; YAZIO stores per one."""
    flat = flatten_nutrients({"energy_kcal": 250, "carb": 30}, basis=100)

    assert flat == {"energy.energy": 2.5, "nutrient.carb": 0.3}


def test_flattening_reads_micronutrients_as_milligrams():
    """The unit `group_nutrients` reports them in, so a value can be handed back."""
    flat = flatten_nutrients({"mineral.calcium": 120, "vitamin.c": 8}, basis=100)

    assert flat["mineral.calcium"] == pytest.approx(0.0012)
    assert flat["vitamin.c"] == pytest.approx(8e-05)


def test_flattening_round_trips_through_grouping():
    grouped = group_nutrients(flatten_nutrients({"energy_kcal": 884, "fat": 100}, basis=100))

    assert grouped["energy_kcal"] == pytest.approx(8.84)
    assert grouped["macros"]["fat"] == pytest.approx(1.0)


@pytest.mark.parametrize("name", ["energy", "energy_kcal", "kcal", "calories"])
def test_energy_may_be_named_any_of_its_aliases(name):
    assert flatten_nutrients({name: 100}, basis=100) == {"energy.energy": 1.0}


@pytest.mark.parametrize("name", ["carb", "carbs", "Carbohydrate", "CARBOHYDRATES"])
def test_carbohydrate_may_be_named_any_of_its_aliases(name):
    assert flatten_nutrients({name: 100}, basis=100) == {"nutrient.carb": 1.0}


def test_an_unknown_nutrient_name_is_refused():
    """The API stores any key at all, so a misspelt one would read back as absent."""
    with pytest.raises(ToolError) as raised:
        flatten_nutrients({"energy_kcal": 100, "fibre": 3}, basis=100)

    assert "'fibre' is not a nutrient" in str(raised.value)


def test_an_unknown_nutrient_family_is_refused():
    with pytest.raises(ToolError):
        flatten_nutrients({"macro.fibre": 3}, basis=100)


def test_an_exact_dotted_key_is_taken_as_given():
    """Anything outside the alias table is named the way YAZIO names it."""
    assert flatten_nutrients({"nutrient.dietaryfiber": 100}, basis=100) == {
        "nutrient.dietaryfiber": 1.0
    }


def test_naming_one_nutrient_twice_is_refused():
    """`carb` and `carbs` are the same key; silently keeping one would hide the other."""
    with pytest.raises(ToolError) as raised:
        flatten_nutrients({"carb": 30, "carbs": 40}, basis=100)

    assert "already given" in str(raised.value)


@pytest.mark.parametrize("value", [-1, "12", None, True])
def test_a_value_that_is_not_a_positive_number_is_refused(value):
    with pytest.raises(ToolError):
        flatten_nutrients({"energy_kcal": value}, basis=100)


def test_a_basis_of_zero_is_refused():
    with pytest.raises(ToolError):
        flatten_nutrients({"energy_kcal": 100}, basis=0)


def test_an_empty_table_is_refused():
    with pytest.raises(ToolError):
        flatten_nutrients({}, basis=100)


def test_a_recipe_round_trip_divides_cleanly():
    """Two ingredients, four portions: per-portion is a quarter of the total."""
    total = sum_nutrients(
        [
            scale_nutrients({"energy.energy": 9.0}, 100),  # 100 g of oil
            scale_nutrients({"energy.energy": 1.0}, 200),  # 200 g of something
        ]
    )
    assert total["energy.energy"] == 1100.0
    assert scale_nutrients(total, 1 / 4)["energy.energy"] == 275.0
