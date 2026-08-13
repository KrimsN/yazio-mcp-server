"""Keeping the logging streak in step with what actually gets tracked.

The YAZIO app computes each day's streak client-side and pushes the result
with `POST /v22/user/streak/{date}` whenever a meal is logged; `GET
/v22/user/streak` only ever reports whatever was last pushed to it, and
nothing on the server advances it on its own. Because this MCP server writes
diary entries straight through the API rather than through the app, that push
never happened here — an account could log food every day and still watch its
streak sit flat, or drop to zero, the way it never would in the app.

`sync_streak` replays that bookkeeping so every tool that adds a diary entry
can call the same function rather than each working out the rules for
itself. It reads yesterday's (and the day before's) recorded state, derives
today's `streak_count`/`freeze_count` from it — freeze recovery included —
unions in the daytime just logged, and pushes the result. Calling it more
than once for the same day is harmless: the counts are derived from
yesterday's state rather than today's own prior entry, so a repeat call
recomputes the same numbers, and the daytimes union just accumulates.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from typing import Any

from mcp.server.fastmcp import Context
from yazio_sdk import AuthenticatedClient
from yazio_sdk.api.streaks import get_streak as api_get_streak
from yazio_sdk.api.streaks import update_streak as api_update_streak
from yazio_sdk.models import StreakUpdate

from .common import plain
from .session import expect_ok, expect_written


async def sync_streak(
    ctx: Context, client: AuthenticatedClient, day: str, daytime: str
) -> dict[str, Any]:
    """Advance the streak for `day` to account for a meal just logged at `daytime`.

    Args:
        ctx: The MCP request context, for error reporting.
        client: An authenticated YAZIO client, already open for the request
            that is logging the meal.
        day: The date the meal was logged against, as YYYY-MM-DD.
        daytime: The meal slot just logged — breakfast, lunch, dinner or snack.
    """
    response = await api_get_streak.asyncio_detailed(client=client)
    calendar = plain(expect_ok(ctx, response, "read the streak calendar")) or {}

    next_state = _next_streak_state(calendar, day, daytime)

    response = await api_update_streak.asyncio_detailed(
        client=client,
        date=day,
        body=StreakUpdate(
            daytimes=next_state["daytimes"],
            streak_count=next_state["streak_count"],
            freeze_count=next_state["freeze_count"],
        ),
    )
    expect_written(ctx, response, f"update the streak for {day}")

    return next_state


def _next_streak_state(
    calendar: dict[str, Any], day: str, daytime: str
) -> dict[str, Any]:
    """Derive the next `streak_count`/`freeze_count`/`daytimes` for `day`.

    Mirrors the rules the app applies client-side: a streak extends from
    yesterday if yesterday has a recorded entry; otherwise it recovers from
    two days back by spending a banked freeze, if yesterday is empty but the
    day before has one to spend; otherwise it restarts at 1, carrying forward
    whatever freeze count the day before already had.

    A `StreakDay` also carries `origin_of_recovery`, whose meaning was never
    observed as non-null in captured traffic — it is left unset here on a
    freeze recovery rather than guessed, since writing the wrong value risks
    the app misreading it.
    """
    today = Date.fromisoformat(day)
    yesterday = calendar.get((today - timedelta(days=1)).isoformat())
    day_before = calendar.get((today - timedelta(days=2)).isoformat())

    if yesterday:
        streak_count = (yesterday.get("streak_count") or 0) + 1
        freeze_count = yesterday.get("freeze_count") or 0
    elif day_before and (day_before.get("freeze_count") or 0) > 0:
        streak_count = (day_before.get("streak_count") or 0) + 1
        freeze_count = (day_before.get("freeze_count") or 0) - 1
    else:
        streak_count = 1
        freeze_count = (day_before.get("freeze_count") or 0) if day_before else 0

    existing_daytimes = set((calendar.get(day) or {}).get("daytimes") or [])
    daytimes = sorted(existing_daytimes | {daytime})

    return {
        "date": day,
        "streak_count": streak_count,
        "freeze_count": freeze_count,
        "daytimes": daytimes,
    }
