"""Tests for app/services/ingest.py's per-league week resolution rewiring (Phase 1).

The root bug: week_number used to be sent to ESPN directly as the literal week number for
both NFL and college, but the two leagues' week numbers are not aligned. These tests pin the
fix: week_number stays the pool's own 1, 2, 3... sequence, each league resolves its own ESPN
week from a calendar anchor date (app/services/calendar.py, tested separately in
test_calendar.py), and a build that truly finds nothing explains exactly why instead of
printing the old dead end "ESPN returned no games for week {N}. Nothing to build yet."
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from sqlalchemy import select

from app.models import Game, Pick, PlatformSetting, Pool, PoolMember, User, Week, WeekEntry
from app.providers import espn
from app.providers.http import cache_put, get_platform_settings
from app.providers.teams import canonical_key
from app.services import ingest, results

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
        # Real by default (Phase 2 remediation, see DECISIONS.md): build_slate now refuses
        # outright when this is None, so a test that actually wants that fallback behavior
        # passes week1_anchor_date=None explicitly, same as it already did before this
        # default existed.
        "week1_anchor_date": dt.date(2026, 9, 12),
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


def test_build_slate_refuses_with_no_anchor_date(db):
    """Phase 2 remediation (see DECISIONS.md): a blank week1_anchor_date used to fall back to
    sending the pool's own week number straight to ESPN for both leagues, which is what
    produced a slate spanning two calendar weeks. build_slate now refuses outright."""
    pool = _pool(db, week1_anchor_date=None)

    report = ingest.build_slate(db, pool, 2026, 1, allow_metered=False)

    assert report.selected == 0
    assert len(report.warnings) == 1
    assert "Set your week 1 anchor date in Settings" in report.warnings[0]
    assert db.scalar(select(Week).where(Week.pool_id == pool.id)) is None


# The global "ESPN only" switch (Phase 5 remediation) --------------------------
#
# Replaces the old per-build commissioner checkbox: build_slate now ANDs its own
# allow_metered parameter (still True by default, unchanged for every existing caller) with
# the site admin's persisted, global PlatformSetting.espn_only, read fresh from the database
# on every call via app.providers.http.get_platform_settings. These two tests prove the AND
# actually gates the metered providers, not just the report's own bookkeeping: neither
# odds_api.fetch_spreads nor cfbd.fetch_lines is ever called while the switch is on, and both
# are called, exactly as before this phase, while it is off.


def test_build_slate_espn_only_switch_on_skips_odds_api_and_cfbd(db, load_fixture, monkeypatch):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    _cache_week(db, load_fixture, "ncaaf", 2026, 2, 1, CFB_SOME_GAMES)

    pool = _pool(db, target_nfl=0, target_ncaaf=4, num_games_per_week=4)
    ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 8, 29))
    db.add(PlatformSetting(espn_only=True))
    db.commit()

    odds_api_calls: list[str] = []
    cfbd_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ingest.odds_api,
        "fetch_spreads",
        lambda db, league, ttl_minutes=None: (odds_api_calls.append(league), ([], "live"))[1],
    )
    monkeypatch.setattr(
        ingest.cfbd,
        "fetch_lines",
        lambda db, year, week, season_type="regular": (
            cfbd_calls.append((year, week)),
            ([], "live"),
        )[1],
    )

    # allow_metered defaults to True here, exactly the value every existing caller (the old
    # commissioner checkbox, app/cli.py, sync_week) already passes or defaults to. The switch
    # alone is what must stop these calls.
    report = ingest.build_slate(db, pool, 2026, 1)

    assert report.candidates == 24
    assert odds_api_calls == []
    assert cfbd_calls == []
    assert any("metered lookups were skipped" in w for w in report.warnings)


def test_build_slate_espn_only_switch_off_calls_odds_api_and_cfbd_as_before(
    db, load_fixture, monkeypatch
):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026_CALENDAR)
    _cache_week(db, load_fixture, "ncaaf", 2026, 2, 1, CFB_SOME_GAMES)

    pool = _pool(db, target_nfl=0, target_ncaaf=4, num_games_per_week=4)
    ingest.ensure_week(db, pool, 2026, 1, anchor_date=dt.date(2026, 8, 29))
    # No PlatformSetting row at all: get_platform_settings creates one on first read, with
    # espn_only defaulting to False, exactly the "off by default" the brief requires.
    assert db.scalar(select(PlatformSetting)) is None

    odds_api_calls: list[str] = []
    cfbd_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ingest.odds_api,
        "fetch_spreads",
        lambda db, league, ttl_minutes=None: (odds_api_calls.append(league), ([], "live"))[1],
    )
    monkeypatch.setattr(
        ingest.cfbd,
        "fetch_lines",
        lambda db, year, week, season_type="regular": (
            cfbd_calls.append((year, week)),
            ([], "live"),
        )[1],
    )

    report = ingest.build_slate(db, pool, 2026, 1)

    assert report.candidates == 24
    assert odds_api_calls == ["ncaaf"]
    assert cfbd_calls == [(2026, 1)]
    assert get_platform_settings(db).espn_only is False


# Test weeks (Phase 3, preseason and test week support) -----------------------


def test_ensure_week_marks_is_test_week_and_labels_it_on_creation(db):
    pool = _pool(db, week1_anchor_date=None)

    week = ingest.ensure_week(
        db, pool, 2026, ingest.TEST_WEEK_NUMBER, anchor_date=dt.date(2026, 8, 10), is_test_week=True
    )

    assert week.is_test_week is True
    assert week.label == "Test week"
    assert week.week_number == 0


def test_ensure_week_does_not_retroactively_flag_an_existing_week(db):
    """is_test_week only ever applies the first time a week row is created, the same
    one-time-on-creation shape a rivalry auto-pin already uses (upsert_games)."""
    pool = _pool(db, week1_anchor_date=dt.date(2026, 9, 12))
    real_week = ingest.ensure_week(db, pool, 2026, 1)
    assert real_week.is_test_week is False

    same_week = ingest.ensure_week(db, pool, 2026, 1, is_test_week=True)

    assert same_week.id == real_week.id
    assert same_week.is_test_week is False


def test_build_slate_test_week_resolves_preseason_without_a_pool_anchor_date(db, load_fixture):
    """A test week needs no pool.week1_anchor_date at all (Phase 2's refusal is for a real
    week only): it resolves against right now instead. August 10, 2026 sits inside NFL's real
    Hall of Fame Weekend window in the recorded calendar (season type 1, week 1)."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_week(db, load_fixture, "nfl", 2026, 1, 1, NFL_SOME_GAMES)

    pool = _pool(db, week1_anchor_date=None, target_nfl=4, target_ncaaf=0, num_games_per_week=4)

    report = ingest.build_slate(
        db,
        pool,
        2026,
        ingest.TEST_WEEK_NUMBER,
        allow_metered=False,
        is_test_week=True,
        now=dt.datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )

    week = db.scalar(select(Week).where(Week.pool_id == pool.id))
    assert week is not None
    assert week.is_test_week is True
    assert week.anchor_date == dt.date(2026, 8, 10)
    assert week.resolved_weeks["nfl"] == {"week": 1, "season_type": espn.SEASON_TYPE_PRESEASON}
    assert report.candidates > 0
    assert report.selected > 0


def test_build_slate_normal_week_does_not_pull_preseason_on_the_same_date(db, load_fixture):
    """The exact same cached calendar and the exact same effective anchor date as the test
    above (week1_anchor_date is set directly to August 10, so a plain week 1 build resolves
    against it), but is_test_week left at its default False. Neither the regular season nor
    the postseason cover August 10, so this must be a dead end, never a preseason resolution,
    proving the default path genuinely never pulls preseason games."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026_CALENDAR)
    _cache_week(db, load_fixture, "nfl", 2026, 1, 1, NFL_SOME_GAMES)

    pool = _pool(db, week1_anchor_date=dt.date(2026, 8, 10), sports=["nfl"])

    report = ingest.build_slate(db, pool, 2026, 1, allow_metered=False)

    week = db.scalar(select(Week).where(Week.pool_id == pool.id))
    assert week is not None
    assert week.is_test_week is False
    assert week.resolved_weeks["nfl"] is None  # not the preseason week the fixture has cached
    assert report.candidates == 0
    assert report.selected == 0


def test_test_week_can_be_deleted_and_cascades_its_games_picks_and_entries(db):
    pool = _pool(db, week1_anchor_date=None)
    week = ingest.ensure_week(
        db, pool, 2026, ingest.TEST_WEEK_NUMBER, anchor_date=dt.date(2026, 8, 10), is_test_week=True
    )
    user = User(email="alice@example.com", password_hash="x", display_name="Alice")
    db.add(user)
    db.flush()
    db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="member"))
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id="evt-test-1",
        start_time=dt.datetime(2026, 8, 10, 17, 0, tzinfo=UTC),
        home_team="Home Team",
        away_team="Away Team",
        home_abbr="HOM",
        away_abbr="AWY",
        canonical_home_key="nfl:home-test",
        canonical_away_key="nfl:away-test",
        in_slate=True,
        slate_rank=1,
    )
    db.add(game)
    db.flush()
    db.add(
        Pick(
            user_id=user.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=game.id,
            picked_team="home",
            confidence=1,
        )
    )
    db.add(WeekEntry(user_id=user.id, pool_id=pool.id, week_id=week.id, points=0))
    db.commit()

    week_id, game_id = week.id, game.id
    db.delete(db.get(Week, week_id))
    db.commit()

    assert db.get(Week, week_id) is None
    assert db.get(Game, game_id) is None
    assert db.scalar(select(Pick).where(Pick.week_id == week_id)) is None
    assert db.scalar(select(WeekEntry).where(WeekEntry.week_id == week_id)) is None
    # The pool and the user themselves are untouched.
    assert db.get(Pool, pool.id) is not None
    assert db.get(User, user.id) is not None


# Slate span guard (Phase 2 remediation) --------------------------------------


def _spanning_week(db, pool: Pool, *, span_days: int) -> Week:
    """A drafted week with two in-slate games span_days apart, real NFL and college weeks
    resolved so _span_too_wide_message has something to report."""
    week = _week_row(db, pool)
    week.resolved_weeks = {
        "nfl": {"week": 1, "season_type": 2},
        "ncaaf": {"week": 3, "season_type": 2},
    }
    base = dt.datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
    for index, offset in enumerate((0, span_days)):
        db.add(
            Game(
                week_id=week.id,
                league="nfl",
                espn_event_id=f"span-{index}",
                start_time=base + dt.timedelta(days=offset),
                home_team=f"Home {index}",
                away_team=f"Away {index}",
                home_abbr=f"H{index}",
                away_abbr=f"A{index}",
                canonical_home_key=f"nfl:home-{index}",
                canonical_away_key=f"nfl:away-{index}",
                in_slate=True,
                slate_rank=index + 1,
            )
        )
    db.flush()
    return week


def test_publish_week_refuses_a_slate_spanning_more_than_eight_days(db):
    pool = _pool(db)
    week = _spanning_week(db, pool, span_days=17)

    with pytest.raises(ingest.SlateSpanTooWide) as excinfo:
        ingest.publish_week(db, week)

    message = str(excinfo.value)
    assert "spans 17 days" in message
    assert "NFL resolved to week 1" in message
    assert "College resolved to week 3" in message
    assert week.status == "draft"


def test_publish_week_allows_a_normal_slate_within_eight_days(db):
    pool = _pool(db)
    week = _spanning_week(db, pool, span_days=3)

    ingest.publish_week(db, week)

    assert week.status == "open"


def test_slate_span_is_none_with_fewer_than_two_games(db):
    pool = _pool(db)
    week = _week_row(db, pool)
    assert ingest.slate_span(db, week) is None


def test_duplicate_team_warnings_explains_a_dropped_game_with_real_names(db):
    """Phase 2 remediation (see DECISIONS.md): app.slate refuses to select two games sharing
    a team; this explains the drop to the commissioner using real team names, which the pure
    slate module deliberately does not have."""
    pool = _pool(db, target_ncaaf=1, target_nfl=0, num_games_per_week=1, sports=["ncaaf"])
    week = _week_row(db, pool)
    closer = _rivalry_game("evt-close", "Ohio State", "Penn State")
    farther = dataclasses.replace(
        _rivalry_game("evt-far", "Ohio State", "Iowa"),
        kickoff=dt.datetime(2026, 9, 19, 17, 0, tzinfo=UTC),
    )
    ingest.upsert_games(db, week, [closer, farther], {}, pool)

    ingest.apply_slate(db, pool, week, now=dt.datetime(2026, 9, 1, tzinfo=UTC))
    warnings = ingest.duplicate_team_warnings(db, week)

    assert len(warnings) == 1
    assert "Ohio State" in warnings[0]
    assert "left off the slate" in warnings[0]
    on_slate = list(
        db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True)))
    )
    assert [g.espn_event_id for g in on_slate] == ["evt-close"]


# Pinned and rivalry games (Phase 5) -------------------------------------------


def _week_row(db, pool: Pool) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=1,
        label="Week 1",
        status="draft",
    )
    db.add(week)
    db.flush()
    return week


def _rivalry_game(event_id: str, home_name: str, away_name: str) -> espn.EspnGame:
    """A college matchup, real canonical_key(...) output on both sides, no fixture needed."""
    return espn.EspnGame(
        event_id=event_id,
        league="ncaaf",
        kickoff=dt.datetime(2026, 9, 12, 17, 0, tzinfo=UTC),
        home=espn.TeamSide(
            name=home_name,
            abbr=home_name[:3].upper(),
            canonical=canonical_key(home_name, "ncaaf"),
        ),
        away=espn.TeamSide(
            name=away_name,
            abbr=away_name[:3].upper(),
            canonical=canonical_key(away_name, "ncaaf"),
        ),
        status="scheduled",
        winner=None,
    )


_OSU_MICHIGAN = [[canonical_key("Ohio State", "ncaaf"), canonical_key("Michigan", "ncaaf")]]


def test_upsert_games_auto_pins_a_new_rivalry_game_home_order(db):
    pool = _pool(db, rivalries=_OSU_MICHIGAN)
    week = _week_row(db, pool)
    game = _rivalry_game("evt1", "Ohio State", "Michigan")

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].pinned is True


def test_upsert_games_auto_pins_a_new_rivalry_game_away_order(db):
    # The pool's rivalry pair lists Ohio State first, but this game has Michigan at home,
    # Ohio State away: the match still has to fire, that is the whole point of checking
    # both home/away orders.
    pool = _pool(db, rivalries=_OSU_MICHIGAN)
    week = _week_row(db, pool)
    game = _rivalry_game("evt1", "Michigan", "Ohio State")

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].pinned is True


def test_upsert_games_does_not_pin_a_non_rivalry_game(db):
    pool = _pool(db, rivalries=_OSU_MICHIGAN)
    week = _week_row(db, pool)
    game = _rivalry_game("evt1", "Duke", "Wake Forest")

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].pinned is False


def test_upsert_games_with_no_rivalries_configured_never_pins(db):
    pool = _pool(db, rivalries=[])
    week = _week_row(db, pool)
    game = _rivalry_game("evt1", "Ohio State", "Michigan")

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].pinned is False


def test_a_commissioners_manual_unpin_of_a_rivalry_game_survives_a_rebuild(db):
    """Decision (DECISIONS.md, Phase 5): auto-pin only fires the first time a game row is
    created. A commissioner who deliberately un-pins a rivalry game while reviewing the
    slate keeps that choice on every later rebuild of the same event id."""
    pool = _pool(db, rivalries=_OSU_MICHIGAN)
    week = _week_row(db, pool)
    game = _rivalry_game("evt1", "Ohio State", "Michigan")

    created = ingest.upsert_games(db, week, [game], {}, pool)
    assert created[0].pinned is True

    ingest.set_pinned(db, week, created[0].id, False)

    rebuilt = ingest.upsert_games(db, week, [game], {}, pool)

    assert rebuilt[0].pinned is False


def test_upsert_games_matches_a_rivalry_pair_regardless_of_pair_order_in_settings(db):
    # The pool stored [Michigan, Ohio State] (reversed from the constant above); the match
    # still has to fire against a game listing them the other way around.
    pool = _pool(
        db,
        rivalries=[[canonical_key("Michigan", "ncaaf"), canonical_key("Ohio State", "ncaaf")]],
    )
    week = _week_row(db, pool)
    game = _rivalry_game("evt1", "Ohio State", "Michigan")

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].pinned is True


# Moneylines (Phase 8) ----------------------------------------------------------


def test_upsert_games_writes_moneyline_when_the_feed_carries_one(db):
    pool = _pool(db)
    week = _week_row(db, pool)
    game = dataclasses.replace(
        _rivalry_game("evt1", "Ohio State", "Michigan"), home_moneyline=-180, away_moneyline=155
    )

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].home_moneyline == -180
    assert rows[0].away_moneyline == 155


def test_upsert_games_leaves_moneyline_null_when_the_feed_has_none(db):
    pool = _pool(db)
    week = _week_row(db, pool)
    game = _rivalry_game(
        "evt1", "Ohio State", "Michigan"
    )  # home_moneyline/away_moneyline default None

    rows = ingest.upsert_games(db, week, [game], {}, pool)

    assert rows[0].home_moneyline is None
    assert rows[0].away_moneyline is None


def test_upsert_games_never_wipes_a_previously_captured_moneyline(db):
    # Spec 5a: ESPN drops odds once a game goes final. A later sync of the same, now
    # final, game must not erase a moneyline an earlier, still-live sync had captured.
    pool = _pool(db)
    week = _week_row(db, pool)
    with_odds = dataclasses.replace(
        _rivalry_game("evt1", "Ohio State", "Michigan"), home_moneyline=-180, away_moneyline=155
    )
    ingest.upsert_games(db, week, [with_odds], {}, pool)

    without_odds = _rivalry_game("evt1", "Ohio State", "Michigan")
    rows = ingest.upsert_games(db, week, [without_odds], {}, pool)

    assert rows[0].home_moneyline == -180
    assert rows[0].away_moneyline == 155


# Pin toggling and the freeze rule (Phase 5) -----------------------------------


def _picked_game(db, week: Week) -> Game:
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id="evtA",
        start_time=dt.datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        home_team="Home Team",
        away_team="Away Team",
        home_abbr="HOM",
        away_abbr="AWY",
        canonical_home_key="nfl:home-team",
        canonical_away_key="nfl:away-team",
        spread_home=1.0,
        closeness=1.0,
        in_slate=True,
        slate_rank=1,
    )
    db.add(game)
    db.flush()
    return game


def _candidate_game(db, week: Week, event_id: str, **overrides) -> Game:
    """A game in the pool, off the slate by default. spread_home defaults to None,
    since that is exactly the shape add_to_slate/swap_slate_game must now accept."""
    defaults = {
        "week_id": week.id,
        "league": "nfl",
        "espn_event_id": event_id,
        "start_time": dt.datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        "home_team": "Home Team",
        "away_team": "Away Team",
        "home_abbr": "HOM",
        "away_abbr": "AWY",
        "canonical_home_key": f"nfl:home-{event_id}",
        "canonical_away_key": f"nfl:away-{event_id}",
        "spread_home": None,
        "closeness": None,
        "in_slate": False,
    }
    defaults.update(overrides)
    game = Game(**defaults)
    db.add(game)
    db.flush()
    return game


# A resolvable spread ranks a game, it never gates eligibility (Post-launch fixes) -----------


def test_add_to_slate_succeeds_for_a_game_with_no_resolved_spread(db):
    pool = _pool(db, num_games_per_week=2, target_nfl=2, target_ncaaf=0, sports=["nfl"])
    week = _week_row(db, pool)
    candidate = _candidate_game(db, week, "no-line")

    added = ingest.add_to_slate(db, week, candidate.id)

    assert added.in_slate is True
    assert added.spread_home is None


def test_swap_slate_game_succeeds_when_the_incoming_game_has_no_resolved_spread(db):
    pool = _pool(db, num_games_per_week=1, target_nfl=1, target_ncaaf=0, sports=["nfl"])
    week = _week_row(db, pool)
    out_game = _candidate_game(
        db, week, "on-slate", spread_home=1.0, closeness=1.0, in_slate=True, slate_rank=1
    )
    in_game = _candidate_game(db, week, "no-line")

    out_result, in_result = ingest.swap_slate_game(db, week, out_game.id, in_game.id)

    assert out_result.in_slate is False
    assert in_result.in_slate is True
    assert in_result.spread_home is None


def test_set_pinned_does_not_require_can_resize_slate(db):
    """Freeze rule check (spec point 7): pinning a game is always allowed, including once
    picks exist, because it only affects a future rebuild's proposal, it never resizes or
    reorders the slate that is already live. add_to_slate/remove_from_slate/swap_slate_game
    would all raise SlateLocked in this exact setup; set_pinned must not."""
    pool = _pool(db, num_games_per_week=1, target_nfl=1, target_ncaaf=0, sports=["nfl"])
    week = _week_row(db, pool)
    game = _picked_game(db, week)

    user = User(email="player@example.com", password_hash="x", display_name="Player")
    db.add(user)
    db.flush()
    db.add(
        Pick(
            user_id=user.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=game.id,
            picked_team="home",
            confidence=1,
        )
    )
    db.flush()

    assert ingest.can_resize_slate(db, week) is False

    result = ingest.set_pinned(db, week, game.id, True)

    assert result.pinned is True
    # Pinning changed nothing about the live slate itself.
    assert result.in_slate is True
    assert result.slate_rank == 1

    unpinned = ingest.set_pinned(db, week, game.id, False)
    assert unpinned.pinned is False
    assert unpinned.in_slate is True


def test_a_new_rivalry_game_does_not_resize_a_frozen_slate(db, monkeypatch):
    """Freeze rule check (spec point 7): once any pick exists, build_slate's locked-out
    branch still runs upsert_games (a genuinely brand new game can still auto-pin itself,
    for example a rivalry matchup that was not part of the original candidate pool), but it
    never calls apply_slate. So that pin can mark itself pinned for a future week, but it
    cannot grow or reshuffle the slate players have already picked against right now."""
    pool = _pool(
        db,
        rivalries=_OSU_MICHIGAN,
        num_games_per_week=1,
        target_nfl=0,
        target_ncaaf=1,
        sports=["ncaaf"],
    )
    week = _week_row(db, pool)
    on_slate = Game(
        week_id=week.id,
        league="ncaaf",
        espn_event_id="already-on-slate",
        start_time=dt.datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        home_team="Duke",
        away_team="Wake Forest",
        home_abbr="DUKE",
        away_abbr="WAKE",
        canonical_home_key=canonical_key("Duke", "ncaaf"),
        canonical_away_key=canonical_key("Wake Forest", "ncaaf"),
        spread_home=1.0,
        closeness=1.0,
        in_slate=True,
        slate_rank=1,
    )
    db.add(on_slate)
    db.flush()

    user = User(email="player3@example.com", password_hash="x", display_name="Player 3")
    db.add(user)
    db.flush()
    db.add(
        Pick(
            user_id=user.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=on_slate.id,
            picked_team="home",
            confidence=1,
        )
    )
    db.flush()

    rivalry_game = _rivalry_game("evt-rivalry", "Ohio State", "Michigan")
    monkeypatch.setattr(ingest, "fetch_candidates", lambda db, pool, week: ([rivalry_game], []))

    report = ingest.build_slate(db, pool, pool.season_year, week.week_number, allow_metered=False)

    assert report.locked_out is True
    new_row = db.scalar(
        select(Game).where(Game.week_id == week.id, Game.espn_event_id == "evt-rivalry")
    )
    assert new_row is not None
    assert new_row.pinned is True  # auto-pin still fires the moment the row is created
    assert new_row.in_slate is False  # but the live, frozen slate itself is untouched
    assert report.selected == 1  # the game count genuinely did not move


# The cron path: build, fetch results, score, run three times in a row (Phase 9b) ------------


def test_the_build_fetch_score_pipeline_is_idempotent_across_three_runs(db, load_fixture):
    """The real functions app.cli.run_cron calls per pool per live week, run back to back
    against unchanged upstream data.

    run_cron does, per pool: sync_week (detect_week, then build_slate), then for every week
    still open or locked, fetch_results followed by score_week_for_pool. detect_week itself
    reads the real wall clock and is covered on its own in this file and in
    tests/test_calendar.py, so this test drives build_slate's own constituent steps directly
    with an explicit week number (week1_anchor_date left unset, the documented pre-anchor
    fallback that sends the week number straight to both leagues) rather than routing through
    sync_week, which is what keeps this test's outcome independent of what day pytest happens
    to run.

    Phase 2 remediation (see DECISIONS.md) made build_slate itself refuse outright when
    week1_anchor_date is None, exactly to close off this fallback for a real build. This test
    still wants to exercise the fallback, so run_once below calls ensure_week,
    fetch_candidates, resolve_spreads, upsert_games and apply_slate directly instead of
    build_slate, a direct continuation of this test's already-stated reason for bypassing the
    higher level sync_week to begin with.

    Run 1 to run 2 may legitimately change something, this is the first time results are
    fetched and scored. Run 2 to run 3, against the exact same cached ESPN and CFBD
    responses, must be a true no-op: the same Week row, the same Game rows with the same
    values, the same WeekEntry rows with the same values, nothing duplicated.
    """
    pool = _pool(
        db,
        week1_anchor_date=None,
        num_games_per_week=20,
        target_nfl=8,
        target_ncaaf=12,
    )
    commissioner = User(
        email="cron-idempotency@example.com",
        password_hash="x",
        display_name="Commissioner",
        role="player",
    )
    db.add(commissioner)
    db.flush()
    db.add(PoolMember(pool_id=pool.id, user_id=commissioner.id, role_in_pool="commissioner"))
    db.flush()

    year, week_number = 2025, 5
    _cache_week(db, load_fixture, "nfl", year, 2, week_number, "espn_nfl_2025_w5.json")
    _cache_week(db, load_fixture, "ncaaf", year, 2, week_number, "espn_cfb_2025_w5.json")
    for event_id, payload in load_fixture("espn_core_odds_nfl_2025_w5.json").items():
        cache_put(db, f"espn:coreodds:nfl:{event_id}", payload)
    cache_put(
        db, f"cfbd:lines:{year}:regular:{week_number}", load_fixture("cfbd_lines_2025_w5.json")
    )

    def run_once() -> Week:
        week = ingest.ensure_week(db, pool, year, week_number)
        games, _attempts = ingest.fetch_candidates(db, pool, week)
        spreads, _warnings = ingest.resolve_spreads(db, week, games, allow_metered=True)
        ingest.upsert_games(db, week, games, spreads, pool)
        ingest.apply_slate(db, pool, week, now=None)
        if week.status == "draft":
            # This fixture's NFL and college week 5 games genuinely span 11 days once both
            # leagues' week numbers are sent directly (the pre-anchor fallback under test
            # here), which Phase 2's span guard correctly refuses to publish. That guard is
            # covered on its own elsewhere; this test only cares that the results/scoring
            # pipeline is idempotent, so it opens the week directly rather than through
            # ingest.publish_week.
            week.status = "open"
            week.published_at = dt.datetime.now(UTC)
        results.fetch_results(db, pool, week)
        results.score_week_for_pool(db, pool, week)
        db.flush()
        return week

    def _snapshot(week: Week) -> tuple[dict, dict]:
        games = {
            g.id: (g.status, g.home_score, g.away_score, g.winner, g.in_slate, g.slate_rank)
            for g in db.scalars(select(Game).where(Game.week_id == week.id))
        }
        entries = {
            e.user_id: (e.points, e.correct, e.possible, e.did_not_submit, e.is_winner)
            for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id))
        }
        return games, entries

    week_run1 = run_once()
    games_1, entries_1 = _snapshot(week_run1)

    week_run2 = run_once()
    games_2, entries_2 = _snapshot(week_run2)

    week_run3 = run_once()
    games_3, entries_3 = _snapshot(week_run3)

    # No duplicate Week row across three runs of the pipeline.
    assert week_run1.id == week_run2.id == week_run3.id

    # No duplicate Game or WeekEntry rows.
    assert len(games_1) == len(games_2) == len(games_3) > 0
    assert len(entries_1) == len(entries_2) == len(entries_3) > 0

    # Run 2 to run 3, against unchanged upstream data, is a true no-op.
    assert games_3 == games_2
    assert entries_3 == entries_2
