"""Tests for app/services/scenarios.py, the database-touching caller for app/scenarios.py
(Phase 8).

Unlike test_scenarios.py (pure, no database) these go through a real session: building
Pool/Week/Game/PoolMember/Pick rows and checking that the caller pulls them correctly,
respects the panel visibility threshold, and caches (and correctly invalidates) per the
module's own documented key.
"""

from __future__ import annotations

import datetime as dt

from app.models import Game, Pick, Pool, PoolMember, User, Week
from app.services import scenarios as scenarios_service

UTC = dt.UTC


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"SCCODE{id(overrides) % 100000}",
        "season_year": 2026,
        "sports": ["nfl", "ncaaf"],
        "timezone": "America/New_York",
        "scoring_mode": "standard",
        "picks_required": 2,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _user(db, name: str) -> User:
    user = User(email=f"{name.lower()}@example.com", password_hash="x", display_name=name)
    db.add(user)
    db.flush()
    return user


def _member(db, pool: Pool, user: User) -> PoolMember:
    member = PoolMember(pool_id=pool.id, user_id=user.id)
    db.add(member)
    db.flush()
    return member


def _week(db, pool: Pool) -> Week:
    week = Week(pool_id=pool.id, season_year=pool.season_year, week_number=1, label="Week 1")
    db.add(week)
    db.flush()
    return week


def _game(
    db, week: Week, *, event_id: str, status: str, winner: str | None = None, **extra
) -> Game:
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id=event_id,
        start_time=dt.datetime(2026, 9, 13, 17, 0, tzinfo=UTC),
        home_team="Home Team",
        away_team="Away Team",
        home_abbr="HOM",
        away_abbr="AWY",
        canonical_home_key=f"nfl-home-{event_id}",
        canonical_away_key=f"nfl-away-{event_id}",
        status=status,
        winner=winner,
        in_slate=True,
        **extra,
    )
    db.add(game)
    db.flush()
    return game


def _pick(db, user: User, pool: Pool, week: Week, game: Game, side: str, confidence: int) -> Pick:
    p = Pick(
        user_id=user.id,
        pool_id=pool.id,
        week_id=week.id,
        game_id=game.id,
        picked_team=side,
        confidence=confidence,
    )
    db.add(p)
    db.flush()
    return p


# panel_thresholds_met -----------------------------------------------------------


def test_panel_thresholds_met_false_below_final_games_threshold(db):
    pool = _pool(db, scenarios_min_final_games=5, scenarios_min_remaining_games=1)
    week = _week(db, pool)
    games = [_game(db, week, event_id=str(i), status="final", winner="home") for i in range(4)]
    games.append(_game(db, week, event_id="rem", status="scheduled"))
    visible, final_count, remaining_count = scenarios_service.panel_thresholds_met(pool, games)
    assert visible is False
    assert final_count == 4
    assert remaining_count == 1


def test_panel_thresholds_met_false_below_remaining_games_threshold(db):
    pool = _pool(db, scenarios_min_final_games=5, scenarios_min_remaining_games=1)
    week = _week(db, pool)
    games = [_game(db, week, event_id=str(i), status="final", winner="home") for i in range(5)]
    visible, final_count, remaining_count = scenarios_service.panel_thresholds_met(pool, games)
    assert visible is False
    assert final_count == 5
    assert remaining_count == 0


def test_panel_thresholds_met_true_once_both_thresholds_are_reached(db):
    pool = _pool(db, scenarios_min_final_games=5, scenarios_min_remaining_games=1)
    week = _week(db, pool)
    games = [_game(db, week, event_id=str(i), status="final", winner="home") for i in range(5)]
    games.append(_game(db, week, event_id="rem", status="scheduled"))
    visible, final_count, remaining_count = scenarios_service.panel_thresholds_met(pool, games)
    assert visible is True


def test_panel_thresholds_met_void_games_count_as_final():
    pool = Pool(
        name="p",
        join_code="X",
        season_year=2026,
        scenarios_min_final_games=2,
        scenarios_min_remaining_games=1,
    )
    games = [
        Game(
            week_id=1,
            league="nfl",
            espn_event_id="1",
            start_time=dt.datetime(2026, 9, 13, tzinfo=UTC),
            home_team="H",
            away_team="A",
            home_abbr="H",
            away_abbr="A",
            canonical_home_key="h",
            canonical_away_key="a",
            status="void",
        ),
        Game(
            week_id=1,
            league="nfl",
            espn_event_id="2",
            start_time=dt.datetime(2026, 9, 13, tzinfo=UTC),
            home_team="H",
            away_team="A",
            home_abbr="H",
            away_abbr="A",
            canonical_home_key="h",
            canonical_away_key="a",
            status="final",
            winner="home",
        ),
        Game(
            week_id=1,
            league="nfl",
            espn_event_id="3",
            start_time=dt.datetime(2026, 9, 13, tzinfo=UTC),
            home_team="H",
            away_team="A",
            home_abbr="H",
            away_abbr="A",
            canonical_home_key="h",
            canonical_away_key="a",
            status="scheduled",
        ),
    ]
    visible, final_count, remaining_count = scenarios_service.panel_thresholds_met(pool, games)
    assert final_count == 2
    assert remaining_count == 1
    assert visible is True


# week_scenario_panel -------------------------------------------------------------


def _setup_two_player_week(db, **pool_overrides):
    defaults = {"scenarios_min_final_games": 1, "scenarios_min_remaining_games": 1}
    defaults.update(pool_overrides)
    pool = _pool(db, **defaults)
    week = _week(db, pool)
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)

    final_game = _game(db, week, event_id="final1", status="final", winner="home")
    remaining_game = _game(db, week, event_id="rem1", status="scheduled")

    _pick(db, alice, pool, week, final_game, "home", 5)  # correct
    _pick(db, alice, pool, week, remaining_game, "home", 3)
    _pick(db, bob, pool, week, final_game, "away", 5)  # wrong
    _pick(db, bob, pool, week, remaining_game, "away", 3)
    return pool, week, alice, bob, final_game, remaining_game


def test_week_scenario_panel_not_visible_below_threshold(db):
    pool, week, *_ = _setup_two_player_week(db, scenarios_min_final_games=5)
    data = scenarios_service.week_scenario_panel(db, pool, week)
    assert data.visible is False
    assert data.report is None
    assert data.min_final_games == 5


def test_week_scenario_panel_visible_computes_a_real_report(db):
    pool, week, alice, bob, final_game, remaining_game = _setup_two_player_week(db)
    data = scenarios_service.week_scenario_panel(db, pool, week, representative_for=[alice.id])
    assert data.visible is True
    assert data.report is not None
    assert data.report.remaining_game_ids == [remaining_game.id]
    assert data.remaining_games == [remaining_game]

    # Alice already has 5 (from the final game); win or lose the remaining game she stays
    # ahead of Bob (who has 0 from the final game and can score at most 3 more).
    alice_outlook = data.report.players[alice.id]
    assert alice_outlook.clinched[1] is True


def test_week_scenario_panel_includes_a_no_show_member(db):
    pool = _pool(db, scenarios_min_final_games=1, scenarios_min_remaining_games=1, picks_required=1)
    week = _week(db, pool)
    alice = _user(db, "Alice")
    ghost = _user(db, "Ghost")
    _member(db, pool, alice)
    _member(db, pool, ghost)  # never picks anything

    final_game = _game(db, week, event_id="f1", status="final", winner="home")
    _game(db, week, event_id="r1", status="scheduled")
    _pick(db, alice, pool, week, final_game, "home", 1)

    data = scenarios_service.week_scenario_panel(db, pool, week)
    assert set(data.report.players.keys()) == {alice.id, ghost.id}
    # Under standard mode a no-show scores 0 and can never win (weekly_winner_ids' rule);
    # this module's local ranking does not replicate that exclusion (see app/scenarios.py,
    # rank_players's docstring), it ranks purely on points, so a real assertion here is
    # just that the ghost is present with a real, non-crashing outlook.
    assert data.report.players[ghost.id].pct_at_place[1] >= 0


def test_week_scenario_panel_caches_identical_calls(db):
    pool, week, alice, bob, final_game, remaining_game = _setup_two_player_week(db)
    first = scenarios_service.week_scenario_panel(db, pool, week)
    second = scenarios_service.week_scenario_panel(db, pool, week)
    assert first.report is second.report  # same object: served from cache, not recomputed


def test_week_scenario_panel_cache_invalidates_when_a_game_goes_final(db):
    pool, week, alice, bob, final_game, remaining_game = _setup_two_player_week(db)
    # A second remaining game keeps the panel visible (remaining_count >= 1) after the
    # first one below goes final, so this exercises cache invalidation specifically,
    # rather than also crossing back below the visibility threshold.
    other_remaining = _game(db, week, event_id="rem2", status="scheduled")
    _pick(db, alice, pool, week, other_remaining, "home", 1)
    _pick(db, bob, pool, week, other_remaining, "away", 1)

    first = scenarios_service.week_scenario_panel(db, pool, week)
    first_ids = set(first.report.remaining_game_ids)
    assert first_ids == {remaining_game.id, other_remaining.id}

    remaining_game.status = "final"
    remaining_game.winner = "home"
    db.flush()
    second = scenarios_service.week_scenario_panel(db, pool, week)

    assert first.report is not second.report
    assert second.report.remaining_game_ids == [other_remaining.id]


def test_week_scenario_panel_moneyline_reads_from_the_game_row(db):
    pool = _pool(db, scenarios_min_final_games=0, scenarios_min_remaining_games=1)
    week = _week(db, pool)
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    remaining_game = _game(
        db,
        week,
        event_id="r1",
        status="scheduled",
        home_moneyline=-300,
        away_moneyline=250,
    )
    _pick(db, alice, pool, week, remaining_game, "home", 5)
    _pick(db, bob, pool, week, remaining_game, "away", 5)

    even = scenarios_service.week_scenario_panel(db, pool, week, probability_model="even")
    moneyline = scenarios_service.week_scenario_panel(db, pool, week, probability_model="moneyline")

    assert even.report.players[alice.id].pct_at_place[1] == 0.5
    assert moneyline.report.players[alice.id].pct_at_place[1] > 0.5  # home is the favorite


# custom_scenario_standings --------------------------------------------------------


def test_custom_scenario_standings_attaches_display_names_and_ranks(db):
    pool, week, alice, bob, final_game, remaining_game = _setup_two_player_week(db)
    rows = scenarios_service.custom_scenario_standings(db, pool, week, {remaining_game.id: "home"})
    by_name = {r.display_name: r for r in rows}
    assert by_name["Alice"].row.points == 8  # 5 (final, correct) + 3 (remaining, correct)
    assert by_name["Bob"].row.points == 0  # both wrong
    assert by_name["Alice"].row.place == 1
    assert by_name["Bob"].row.place == 2


def test_custom_scenario_standings_undecided_game_omitted(db):
    pool, week, alice, bob, final_game, remaining_game = _setup_two_player_week(db)
    rows = scenarios_service.custom_scenario_standings(db, pool, week, {remaining_game.id: None})
    by_name = {r.display_name: r for r in rows}
    assert by_name["Alice"].row.points == 5  # only the final game counts
    assert by_name["Alice"].row.possible == 1
