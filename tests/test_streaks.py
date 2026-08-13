"""The streak/freeze arithmetic `sync_streak` derives, and its wiring into food logging.

The endpoint that reports a streak's state (`GET /v22/user/streak`) only ever
echoes back whatever was last pushed to it, and the rules for what to push —
extend, recover with a freeze, or restart — are undocumented and only worked
out from observed calendars. Both are pinned down here, away from the
network: the arithmetic in isolation, and the fact that logging a product or
a recipe triggers it in the first place.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yazio_mcp.server import build_server
from yazio_mcp.streaks import _next_streak_state, sync_streak
from yazio_mcp.tools import tracking


def day(streak_count: float, freeze_count: float, daytimes: list[str] | None = None) -> dict:
    """One calendar entry as GET /v22/user/streak reports it."""
    return {
        "streak_count": streak_count,
        "freeze_count": freeze_count,
        "daytimes": daytimes or [],
    }


# -- _next_streak_state: the arithmetic in isolation ------------------------


def test_a_streak_extends_from_yesterday():
    calendar = {"2026-08-12": day(4, 0, ["breakfast"])}

    state = _next_streak_state(calendar, "2026-08-13", "lunch")

    assert state["streak_count"] == 5
    assert state["freeze_count"] == 0


def test_yesterdays_freeze_count_carries_forward_unspent():
    calendar = {"2026-08-12": day(4, 2)}

    state = _next_streak_state(calendar, "2026-08-13", "breakfast")

    assert state["freeze_count"] == 2


def test_a_gap_recovers_from_two_days_back_by_spending_a_freeze():
    """Yesterday (08-12) has no entry, but the day before does and has a freeze."""
    calendar = {"2026-08-11": day(9, 1)}

    state = _next_streak_state(calendar, "2026-08-13", "breakfast")

    assert state["streak_count"] == 10
    assert state["freeze_count"] == 0


def test_a_gap_without_a_banked_freeze_restarts_the_streak():
    calendar = {"2026-08-11": day(9, 0)}

    state = _next_streak_state(calendar, "2026-08-13", "breakfast")

    assert state["streak_count"] == 1
    assert state["freeze_count"] == 0


def test_a_streak_with_no_history_at_all_starts_at_one():
    state = _next_streak_state({}, "2026-08-13", "breakfast")

    assert state["streak_count"] == 1
    assert state["freeze_count"] == 0


def test_todays_existing_daytimes_are_unioned_with_the_new_one():
    calendar = {"2026-08-13": day(0, 0, ["breakfast"])}

    state = _next_streak_state(calendar, "2026-08-13", "lunch")

    assert set(state["daytimes"]) == {"breakfast", "lunch"}


def test_logging_the_same_daytime_twice_does_not_duplicate_it():
    calendar = {"2026-08-13": day(1, 0, ["breakfast"])}

    state = _next_streak_state(calendar, "2026-08-13", "breakfast")

    assert state["daytimes"] == ["breakfast"]


# -- sync_streak: reading the calendar and pushing the result ---------------


@pytest.fixture
def streak_api(monkeypatch):
    """Answer the streak endpoints locally, capturing what gets pushed."""
    from yazio_mcp import streaks

    state = SimpleNamespace(calendar={}, pushed=[])

    async def fake_get(*, client, **kwargs):
        return SimpleNamespace(status_code=200, parsed=dict(state.calendar), content=b"")

    async def fake_update(*, client, date, body, **kwargs):
        state.pushed.append((date, body))
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    monkeypatch.setattr(streaks.api_get_streak, "asyncio_detailed", fake_get, raising=False)
    monkeypatch.setattr(streaks.api_update_streak, "asyncio_detailed", fake_update, raising=False)

    return state


@pytest.mark.asyncio
async def test_sync_streak_pushes_the_state_derived_from_the_calendar(streak_api):
    streak_api.calendar = {"2026-08-12": day(4, 1)}

    result = await sync_streak(None, object(), "2026-08-13", "lunch")

    assert result["streak_count"] == 5
    assert result["freeze_count"] == 1
    assert result["daytimes"] == ["lunch"]

    pushed_date, pushed_body = streak_api.pushed[0]
    assert pushed_date == "2026-08-13"
    assert pushed_body.streak_count == 5
    assert pushed_body.freeze_count == 1
    assert pushed_body.daytimes == ["lunch"]


# -- wiring: track_product and track_recipe both call sync_streak -----------


@pytest.fixture
def food_api(monkeypatch):
    """Fake the diary write and product fetch that the tracking tools need,
    and capture what each passes to sync_streak in its place."""
    calls: list[tuple[str, str]] = []

    @asynccontextmanager
    async def fake_client(ctx):
        yield object()

    async def fake_fetch_product(client, product_id):
        return {"name": "Apple", "base_unit": "g", "servings": []}

    async def fake_add(*, client, body, **kwargs):
        return SimpleNamespace(status_code=204, parsed=None, content=b"")

    async def fake_sync_streak(ctx, client, day, daytime):
        calls.append((day, daytime))
        return {"date": day, "streak_count": 3, "freeze_count": 0, "daytimes": [daytime]}

    monkeypatch.setattr(tracking, "yazio_client", fake_client)
    monkeypatch.setattr(tracking, "fetch_product", fake_fetch_product)
    monkeypatch.setattr(
        tracking.api_add_consumed_items, "asyncio_detailed", fake_add, raising=False
    )
    monkeypatch.setattr(tracking, "sync_streak", fake_sync_streak)

    return calls


@pytest.mark.asyncio
async def test_tracking_a_product_syncs_the_streak_for_its_daytime(food_api):
    result = await build_server().call_tool(
        "track_product",
        {"product_id": "p1", "daytime": "breakfast", "amount": 100, "date": "2026-08-13"},
    )

    assert food_api == [("2026-08-13", "breakfast")]
    assert result[1]["streak"]["streak_count"] == 3


@pytest.mark.asyncio
async def test_tracking_a_recipe_syncs_the_streak_too(food_api):
    result = await build_server().call_tool(
        "track_recipe",
        {"recipe_id": "r1", "daytime": "dinner", "date": "2026-08-13"},
    )

    assert food_api == [("2026-08-13", "dinner")]
    assert result[1]["streak"]["streak_count"] == 3
