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

from sqlalchemy import select

from app.models import Game, Pick, Pool, User, Week
from app.providers import espn
from app.providers.http import cache_put
from app.providers.teams import canonical_key
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
