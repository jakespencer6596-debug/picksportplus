"""Resolve each of a pool's leagues to an ESPN week and season type from a calendar date.

The root problem this module fixes: NFL and college football week numbers are not aligned.
College week 1 starts in late August, NFL week 1 the second week of September, and they stay
two to three weeks apart all season. College also has bowl season, which NFL has no
equivalent of on the same calendar. Sending one "week_number" straight to ESPN for both
leagues (the old behaviour, still in git history) is wrong for one league on almost every
date of the season.

This module does not do its own date-window arithmetic. It leans entirely on the calendar
parsing already in app/providers/espn.py (parse_calendar, current_week_from_payload), which
reads the exact ESPN calendar shape documented in SPEC.md Section 5a. All this module adds is
the "try the regular season, then fall back to the postseason, then give up cleanly" policy,
plus a caching call to ESPN that stays inside one HTTP request per league per season.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Pool
from app.providers import espn
from app.providers.http import ProviderError

log = logging.getLogger("picksportplus.calendar")

# The calendar block on an ESPN scoreboard payload covers every week of every season type for
# the year, regardless of which week the scoreboard itself defaulted to. So one fetch per
# league per season is enough, and a generous ttl is what keeps repeat resolutions free.
CALENDAR_TTL_MINUTES = 60


@dataclass(frozen=True)
class LeagueResolution:
    """What one league's calendar resolved to for a pool week's anchor date."""

    league: str
    week: int
    season_type: int
    label: str
    start: dt.datetime
    end: dt.datetime

    @property
    def is_postseason(self) -> bool:
        return self.season_type == espn.SEASON_TYPE_POSTSEASON


def default_week1_anchor_date(year: int) -> dt.date:
    """The second Saturday of September, NFL week 1 in a normal season (Phase 2 remediation,
    see DECISIONS.md). Used to prefill a new league's anchor date and to backfill a pool that
    predates the anchor date becoming a required field."""
    first_of_september = dt.date(year, 9, 1)
    days_to_first_saturday = (5 - first_of_september.weekday()) % 7  # Saturday = 5
    first_saturday = first_of_september + dt.timedelta(days=days_to_first_saturday)
    return first_saturday + dt.timedelta(days=7)


def anchor_datetime(anchor_date: dt.date) -> dt.datetime:
    """Midday UTC on the anchor date.

    Calendar week windows in the recorded payloads flip over at 07:00Z, not at midnight in any
    particular US timezone, so midday UTC sits safely inside whichever week the anchor Saturday
    belongs to on either coast.
    """
    return dt.datetime.combine(anchor_date, dt.time(hour=12), tzinfo=dt.UTC)


def resolve_league_week(
    db: Session, league: str, year: int, anchor_date: dt.date
) -> LeagueResolution | None:
    """One league's ESPN week and season type for a pool week anchored on anchor_date.

    Tries the regular season first, then the postseason (the playoffs for NFL, bowl season
    for college). Returns None when the anchor date genuinely falls outside both, for example
    NFL during the first weeks of the college season, or college deep into the NFL postseason.
    Never raises: a provider outage degrades to None here too, so the caller can still build a
    slate from whichever leagues did resolve.
    """
    try:
        # week and season_type are left at their defaults: the calendar block that comes back
        # covers every season type regardless of what is asked for, and no specific week's
        # events are needed here, only the calendar.
        payload = espn.fetch_scoreboard(db, league, year, ttl_minutes=CALENDAR_TTL_MINUTES)
    except ProviderError as exc:
        log.warning("could not load the %s calendar for %s: %s", league, year, exc)
        return None

    now = anchor_datetime(anchor_date)
    for season_type in (espn.SEASON_TYPE_REGULAR, espn.SEASON_TYPE_POSTSEASON):
        week = espn.current_week_from_payload(payload, now=now, season_type=season_type)
        # current_week_from_payload falls back to the next upcoming week, or holds on the last
        # week, when now is not actually inside any window for that season type. Either
        # fallback means the anchor date is not really in this season type, so only a genuine
        # containment counts as a resolution here.
        if week is not None and week.start <= now < week.end:
            return LeagueResolution(
                league=league,
                week=week.week,
                season_type=season_type,
                label=week.label,
                start=week.start,
                end=week.end,
            )
    return None


def resolve_pool_weeks(
    db: Session, pool: Pool, anchor_date: dt.date
) -> dict[str, LeagueResolution | None]:
    """Every league the pool plays, resolved for one anchor date.

    A league that maps to None had no games for that date in either the regular season or the
    postseason. That is not an error, it means the caller should build the week from whatever
    leagues did resolve.
    """
    return {
        league: resolve_league_week(db, league, pool.season_year, anchor_date)
        for league in pool.sports or ["nfl", "ncaaf"]
    }
