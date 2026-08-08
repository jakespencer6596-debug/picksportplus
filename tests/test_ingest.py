"""Tests for app/services/ingest.py's per-league week resolution rewiring (Phase 1).

The root bug: week_number used to be sent to ESPN directly as the literal week number for
both NFL and college, but the two leagues' week numbers are not aligned. These tests pin the
fix: week_number stays the pool's own 1, 2, 3... sequence, each league resolves its own ESPN
week from a calendar anchor date (app/services/calendar.py, tested separately in
test_calendar.py), and a build that truly finds nothing explains exactly why instead of
printing the old dead end "ESPN returned no games for week {N}. Nothing to build yet."
"""

from __future__ import annotations

import datetime as dt

from app.models import Pool
from app.providers.http import cache_put
from app.services import ingest

UTC = dt.UTC

NFL_2026_CALENDAR = "espn_nfl_upcoming_with_odds.json"
CFB_2026_CALENDAR = "espn_cfb_2026_calendar.json"
# Stand-ins for "some games came back at the resolved week". Content does not need to match
# the resolved week's real matchups, only to exercise the plumbing: cache key routing, game
# counts landing on the report, and resolved_weeks/is_bowl_week bookkeeping.
NFL_SOME_GAMES = "espn_nfl_2025_w5.json"
CFB_SOME_GAMES = "espn_cfb_2025_w5.json"


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"CODE{id(overrides) % 100000}",
        "season_year": 2026,
        "num_games_per_week": 4,
        "target_nfl": 2,
        "target_ncaaf": 2,
        "sports": ["nfl", "ncaaf"],
        "auto_publish": False,
        "timezone": "America/New_York",
        "current_week": 1,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _cache_calendar(db, load_fixture, league: str, year: int, filename: str) -> None:
    cache_put(db, f"espn:scoreboard:{league}:{year}:2:None", load_fixture(filename))


def _cache_week(
    db, load_fixture, league: str, year: int, season_type: int, week: int, filename: str
) -> None:
    cache_put(db, f"espn:scoreboard:{league}:{year}:{season_type}:{week}", load_fixture(filename))


# ensure_week: anchor computation ---------------------------------------------


def test_ensure_week_computes_anchor_from_pool_week1_anchor_date(db):
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))

    week = ingest.ensure_week(db, pool, 2026, 3)

    assert week.anchor_date == dt.date(2026, 9, 26)  # week1 + 2 weeks
    assert week.week_number == 3


def test_ensure_week_leaves_anchor_none_without_a_configured_pool(db):
    pool = _pool(db, week1_anchor_date=None)

    week = ingest.ensure_week(db, pool, 2026, 1)

    assert week.anchor_date is None


def test_ensure_week_explicit_override_wins(db):
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))

    week = ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2027, 3, 6))

    assert week.anchor_date == dt.date(2027, 3, 6)


def test_ensure_week_is_idempotent_and_does_not_move_an_existing_anchor(db):
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))
    first = ingest.ensure_week(db, pool, 2026, 1)
    pool.week1_anchor_date = dt.date(2026, 10, 1)  # commissioner changes the setting later

    second = ingest.ensure_week(db, pool, 2026, 1)

    assert second.id == first.id
    assert second.anchor_date == dt.date(2026, 9, 12)  # unchanged


def test_ensure_week_backfills_an_anchor_onto_a_previously_unanchored_week(db):
    pool = _pool(db, week1_anchor_date=None)
    week = ingest.ensure_week(db, pool, 2026, 1)
    assert week.anchor_date is None

    backfilled = ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 9, 12))

    assert backfilled.id == week.id
    assert backfilled.anchor_date == dt.date(2026, 9, 12)


# detect_week: anchor arithmetic vs the ESPN fallback --------------------------


def test_detect_week_uses_pure_date_arithmetic_when_anchor_is_configured(db):
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))

    week_number = ingest.detect_week(db, pool, now=dt.datetime(2026, 9, 26, 18, 0, tzinfo=UTC))

    assert week_number == 3  # two weeks after the week 1 anchor


def test_detect_week_before_the_anchor_returns_week_one(db):
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))

    week_number = ingest.detect_week(db, pool, now=dt.datetime(2026, 8, 1, tzinfo=UTC))

    assert week_number == 1


def test_detect_week_with_an_anchor_makes_no_espn_call(db, load_fixture):
    """Pure arithmetic, so nothing is cached and nothing is fetched."""
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))

    ingest.detect_week(db, pool, now=dt.datetime(2026, 9, 20, tzinfo=UTC))

    from app.providers.http import cache_get

    assert cache_get(db, "espn:calendar:nfl:2026") is None


def test_detect_week_falls_back_to_espn_without_an_anchor(db, load_fixture):
    pool = _pool(db, week1_anchor_date=None)
    cache_put(db, "espn:calendar:nfl:2026", load_fixture(NFL_2026_CALENDAR))

    week_number = ingest.detect_week(db, pool, now=dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC))

    assert week_number == 2  # whatever ESPN's own NFL calendar says, per the old behaviour


def test_detect_week_falls_back_to_none_when_espn_has_nothing_cached_either(db):
    pool = _pool(db, week1_anchor_date=None)

    assert ingest.detect_week(db, pool, now=dt.datetime(2026, 9, 20, tzinfo=UTC)) is None


# fetch_candidates: per-league resolution --------------------------------------


def test_fetch_candidates_resolves_each_league_independently(db, load_fixture):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    _cache_week(db, load_fixture, "nfl", 2026, 2, 1, NFL_SOME_GAMES)
    _cache_week(db, load_fixture, "ncaaf", 2026, 2, 3, CFB_SOME_GAMES)

    pool = _pool(db)
    week = ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 9, 12))

    games, attempts = ingest.fetch_candidates(db, pool, week)

    assert {g.league for g in games} == {"nfl", "ncaaf"}
    assert len(games) == 14 + 24
    assert week.resolved_weeks == {
        "nfl": {"week": 1, "season_type": 2},
        "ncaaf": {"week": 3, "season_type": 2},
    }
    assert week.is_bowl_week is False
    assert {a.league for a in attempts} == {"nfl", "ncaaf"}
    assert all(a.error is None for a in attempts)


def test_fetch_candidates_sets_is_bowl_week_when_college_needs_the_postseason(db, load_fixture):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    _cache_week(db, load_fixture, "nfl", 2026, 2, 15, NFL_SOME_GAMES)
    _cache_week(db, load_fixture, "ncaaf", 2026, 3, 1, CFB_SOME_GAMES)

    pool = _pool(db)
    week = ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 12, 19))

    games, _attempts = ingest.fetch_candidates(db, pool, week)

    assert len(games) == 14 + 24
    assert week.is_bowl_week is True
    assert week.resolved_weeks["ncaaf"] == {"week": 1, "season_type": 3}
    assert week.resolved_weeks["nfl"] == {"week": 15, "season_type": 2}


def test_fetch_candidates_league_with_no_games_for_the_window_is_not_an_error(db, load_fixture):
    """NFL has not started when college week 1 kicks off. Not an error: the slate is built
    from whichever leagues did resolve."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    _cache_week(db, load_fixture, "ncaaf", 2026, 2, 1, CFB_SOME_GAMES)

    pool = _pool(db)
    week = ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 8, 29))

    games, attempts = ingest.fetch_candidates(db, pool, week)

    assert {g.league for g in games} == {"ncaaf"}
    assert len(games) == 24
    assert week.resolved_weeks["nfl"] is None
    assert week.resolved_weeks["ncaaf"] == {"week": 1, "season_type": 2}
    nfl_attempt = next(a for a in attempts if a.league == "nfl")
    assert nfl_attempt.resolved_week is None
    assert nfl_attempt.error is None  # not resolving is not a failure


def test_fetch_candidates_falls_back_to_week_number_when_anchor_is_unset(db, load_fixture):
    """A pool with no week1_anchor_date configured: the pre anchor behaviour, preserved."""
    _cache_week(db, load_fixture, "nfl", 2025, 2, 5, NFL_SOME_GAMES)
    _cache_week(db, load_fixture, "ncaaf", 2025, 2, 5, CFB_SOME_GAMES)

    pool = _pool(db, week1_anchor_date=None, season_year=2025)
    week = ingest.ensure_week(db, pool, 2025, 5)
    assert week.anchor_date is None

    games, attempts = ingest.fetch_candidates(db, pool, week)

    assert len(games) == 14 + 24
    assert week.resolved_weeks == {
        "nfl": {"week": 5, "season_type": 2},
        "ncaaf": {"week": 5, "season_type": 2},
    }
    assert week.is_bowl_week is False
    assert all(a.resolved_week == 5 for a in attempts)


# build_slate: the rewritten dead end message ----------------------------------


def test_build_slate_dead_end_message_explains_both_leagues(db, load_fixture):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)

    pool = _pool(db)
    # Pre create the week with an anchor date genuinely outside every calendar in the
    # fixtures for both leagues (see test_calendar.py's equivalent case).
    ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2027, 3, 6))

    report = ingest.build_slate(db, pool, 2026, 1, allow_metered=False)

    assert report.candidates == 0
    assert report.selected == 0
    assert len(report.warnings) == 1
    message = report.warnings[0]

    assert "2027-03-06" in message
    assert "NFL" in message
    assert "College" in message
    assert "no week was resolved" in message
    assert "Valid regular season range" in message
    assert "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard" in message
    assert (
        "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
        in message
    )


def test_build_slate_dead_end_message_distinguishes_a_failed_request_from_no_resolution(
    db, load_fixture
):
    """College resolves a week but the per-week fetch is not cached (ProviderError). NFL does
    not resolve at all. The message must tell those two apart."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    # Deliberately no cache entry for espn:scoreboard:ncaaf:2026:2:1, so that fetch raises.

    pool = _pool(db)
    ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 8, 29))

    report = ingest.build_slate(db, pool, 2026, 1, allow_metered=False)

    assert report.candidates == 0
    message = report.warnings[0]
    assert "no week was resolved" in message  # NFL
    assert "resolved to week 1" in message  # College
    assert "failed" in message


def test_build_slate_succeeds_normally_when_only_one_league_has_no_games(db, load_fixture):
    """A partial resolution (one league None) must not turn into a dead end warning as long
    as at least one league returned games."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    _cache_week(db, load_fixture, "ncaaf", 2026, 2, 1, CFB_SOME_GAMES)

    pool = _pool(db, target_nfl=0, target_ncaaf=4, num_games_per_week=4)
    ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 8, 29))

    report = ingest.build_slate(db, pool, 2026, 1, allow_metered=False)

    assert report.candidates == 24
    assert not any("ESPN returned no games" in w for w in report.warnings)
