"""Tests for app/services/standings.py sort direction (Phase 2).

season_standings and weekly_leaderboard must rank best-first regardless of scoring mode:
highest points first under "standard", lowest points first under "inverse", since under
inverse a low total is the win. _assign_ranks itself does not know or care about direction,
it just walks whatever order the sort produced, so these tests go through the real sort to
prove the direction is actually correct, not just that ranks share correctly once ordered.
"""

from __future__ import annotations

import datetime as dt

from app.models import Pool, PoolMember, User, Week, WeekEntry
from app.services.standings import (
    season_points_ranking,
    season_standings,
    season_wins_ranking,
    weekly_leaderboard,
)

UTC = dt.UTC


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"STCODE{id(overrides) % 100000}",
        "season_year": 2026,
        "sports": ["nfl", "ncaaf"],
        "timezone": "America/New_York",
        "current_week": 1,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _user(db, name: str) -> User:
    user = User(
        email=f"{name.lower().replace(' ', '.')}@example.com",
        password_hash="x",
        display_name=name,
    )
    db.add(user)
    db.flush()
    return user


def _member(db, pool: Pool, user: User, role: str = "member") -> PoolMember:
    member = PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool=role)
    db.add(member)
    db.flush()
    return member


def _week(db, pool: Pool, week_number: int = 1, status: str = "scored") -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=week_number,
        label=f"Week {week_number}",
        status=status,
    )
    db.add(week)
    db.flush()
    return week


def _entry(
    db,
    pool: Pool,
    week: Week,
    user: User,
    *,
    points: int,
    correct: int = 0,
    possible: int = 14,
    is_winner: bool = False,
    did_not_submit: bool = False,
    submitted_at: dt.datetime | None = None,
) -> WeekEntry:
    if submitted_at is None and not did_not_submit:
        submitted_at = dt.datetime.now(UTC)
    entry = WeekEntry(
        pool_id=pool.id,
        week_id=week.id,
        user_id=user.id,
        points=points,
        correct=correct,
        possible=possible,
        is_winner=is_winner,
        did_not_submit=did_not_submit,
        submitted_at=submitted_at,
    )
    db.add(entry)
    db.flush()
    return entry


def _three_players(db, pool: Pool) -> tuple[User, User, User]:
    """Alice 10, Bob 40, Carol 70. Alice has the low score, Carol the high score."""
    alice, bob, carol = _user(db, "Alice"), _user(db, "Bob"), _user(db, "Carol")
    for user in (alice, bob, carol):
        _member(db, pool, user)
    return alice, bob, carol


# season_standings ------------------------------------------------------------


def test_season_standings_inverse_ranks_lowest_points_first(db):
    pool = _pool(db, scoring_mode="inverse")
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, is_winner=True)
    _entry(db, pool, week, bob, points=40)
    _entry(db, pool, week, carol, points=70)

    rows = season_standings(db, pool)

    assert [r.display_name for r in rows] == ["Alice", "Bob", "Carol"]
    assert [r.rank for r in rows] == [1, 2, 3]


def test_season_standings_standard_ranks_highest_points_first(db):
    pool = _pool(db, scoring_mode="standard")
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10)
    _entry(db, pool, week, bob, points=40)
    _entry(db, pool, week, carol, points=70, is_winner=True)

    rows = season_standings(db, pool)

    assert [r.display_name for r in rows] == ["Carol", "Bob", "Alice"]
    assert [r.rank for r in rows] == [1, 2, 3]


def test_season_standings_inverse_tied_low_scores_share_rank_one(db):
    pool = _pool(db, scoring_mode="inverse")
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, is_winner=True)
    _entry(db, pool, week, bob, points=10, is_winner=True)
    _entry(db, pool, week, carol, points=70)

    rows = season_standings(db, pool)
    by_name = {r.display_name: r for r in rows}

    assert by_name["Alice"].rank == 1
    assert by_name["Bob"].rank == 1
    # Competition ranking: the next distinct score skips to rank 3, not 2.
    assert by_name["Carol"].rank == 3


# weekly_leaderboard ------------------------------------------------------------


def test_weekly_leaderboard_inverse_ranks_lowest_points_first(db):
    pool = _pool(db, scoring_mode="inverse")
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, correct=14, possible=14, is_winner=True)
    _entry(db, pool, week, bob, points=40, correct=8, possible=14)
    _entry(db, pool, week, carol, points=70, correct=2, possible=14)

    rows, resolved_week = weekly_leaderboard(db, pool, week=week)

    assert resolved_week.id == week.id
    assert [r.display_name for r in rows] == ["Alice", "Bob", "Carol"]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert rows[0].is_winner is True


def test_weekly_leaderboard_standard_ranks_highest_points_first(db):
    pool = _pool(db, scoring_mode="standard")
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, correct=2, possible=14)
    _entry(db, pool, week, bob, points=40, correct=8, possible=14)
    _entry(db, pool, week, carol, points=70, correct=14, possible=14, is_winner=True)

    rows, _ = weekly_leaderboard(db, pool, week=week)

    assert [r.display_name for r in rows] == ["Carol", "Bob", "Alice"]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert rows[0].is_winner is True


def test_weekly_leaderboard_did_not_submit_flag_reads_straight_from_the_entry(db):
    pool = _pool(db, scoring_mode="inverse")
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, correct=14, possible=14, is_winner=True)
    _entry(db, pool, week, bob, points=105, correct=0, possible=14, did_not_submit=True)

    rows, _ = weekly_leaderboard(db, pool, week=week)
    by_name = {r.display_name: r for r in rows}

    assert by_name["Alice"].did_not_submit is False
    assert by_name["Bob"].did_not_submit is True


def test_weekly_leaderboard_member_with_no_entry_at_all_reads_as_did_not_submit(db):
    # A member who joined after the week was scored (or before any week entry exists at
    # all) has no WeekEntry row for this week. did_not_submit must still read True rather
    # than crash on a missing entry.
    pool = _pool(db, scoring_mode="inverse")
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, correct=14, possible=14, is_winner=True)
    # Bob has no entry row for this week at all.

    rows, _ = weekly_leaderboard(db, pool, week=week)
    bob_row = next(r for r in rows if r.display_name == "Bob")

    assert bob_row.did_not_submit is True
    assert bob_row.points == 0


# Test weeks are quarantined from season standings (Phase 3) -------------------


def test_season_standings_excludes_a_test_weeks_entries(db):
    """A test week scores normally within itself (its own WeekEntry row is real and correct,
    see tests/test_payout_service.py for that half), but must contribute zero to season
    totals, correct counts, and weekly-win counts. Alice's real week 1 win is the only thing
    that should show up in her season row; her (much bigger) test week result must not."""
    pool = _pool(db, scoring_mode="standard")
    alice, bob, carol = _three_players(db, pool)
    real_week = _week(db, pool, week_number=1)
    test_week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=0,
        label="Test week",
        status="scored",
        is_test_week=True,
    )
    db.add(test_week)
    db.flush()

    _entry(db, pool, real_week, alice, points=10, correct=5, possible=14, is_winner=True)
    _entry(db, pool, test_week, alice, points=999, correct=99, possible=99, is_winner=True)

    rows = season_standings(db, pool)
    alice_row = next(r for r in rows if r.display_name == "Alice")

    assert alice_row.points == 10
    assert alice_row.correct == 5
    assert alice_row.possible == 14
    assert alice_row.weeks_played == 1
    assert alice_row.weekly_wins == 1  # the test week's win does not count a second time


# Season tiebreak (Phase 2/3, "Tab entry and season tiebreak") -----------------


def _weeks(db, pool: Pool, count: int) -> list[Week]:
    return [_week(db, pool, week_number=n) for n in range(1, count + 1)]


def test_season_points_ranking_breaks_a_tie_on_weekly_wins(db):
    pool = _pool(db)  # default scoring_mode "inverse", default season_tiebreak_mode "wins"
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, is_winner=True)
    _entry(db, pool, week, bob, points=10, is_winner=False)
    _entry(db, pool, week, carol, points=50)

    rows = season_points_ranking(db, pool)
    by_name = {r.display_name: r for r in rows}

    assert [r.display_name for r in rows] == ["Alice", "Bob", "Carol"]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert by_name["Alice"].tiebreak_reason == "Tiebreak: 1 weekly wins to 0."
    assert by_name["Bob"].tiebreak_reason == "Tiebreak: 0 weekly wins to 1."
    assert by_name["Carol"].tiebreak_reason is None


def test_season_points_ranking_three_way_tie_orders_by_distinct_win_counts(db):
    pool = _pool(db)
    alice, bob, carol = _three_players(db, pool)
    tie_week, w2, w3, w4 = _weeks(db, pool, 4)
    for user in (alice, bob, carol):
        _entry(db, pool, tie_week, user, points=10)
    # Alice: 3 wins, Carol: 2, Bob: 1, all with zero extra points so the season point total
    # stays tied at 10 for everyone.
    for week in (w2, w3, w4):
        _entry(db, pool, week, alice, points=0, is_winner=True)
    for week in (w2, w3):
        _entry(db, pool, week, carol, points=0, is_winner=True)
    _entry(db, pool, w2, bob, points=0, is_winner=True)

    rows = season_points_ranking(db, pool)

    assert [r.display_name for r in rows] == ["Alice", "Carol", "Bob"]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert [r.weekly_wins for r in rows] == [3, 2, 1]


def test_season_wins_ranking_ties_break_on_points_in_the_pools_own_direction(db):
    """Season Wins ladder, two tied on wins, different points. Under the pool's default
    inverse scoring, fewer points finishes higher, mirroring the points ladder's own
    direction rather than a hard coded "fewer always wins.\" """
    pool = _pool(db, scoring_mode="inverse")
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=20, is_winner=True)
    _entry(db, pool, week, bob, points=5, is_winner=True)

    rows = season_wins_ranking(db, pool)
    by_name = {r.display_name: r for r in rows}

    assert [r.display_name for r in rows] == ["Bob", "Alice"]
    assert by_name["Bob"].tiebreak_reason == "Tiebreak: 5 points to 20."
    assert by_name["Alice"].tiebreak_reason == "Tiebreak: 20 points to 5."


def test_season_wins_ranking_sorts_wins_descending_even_under_inverse_scoring(db):
    """The single easiest wiring mistake here (see app/payouts.py's own module docstring):
    proves the PRIMARY sort is weekly wins, never the pool's scoring direction, by giving the
    player with more wins the far worse (higher) inverse points total."""
    pool = _pool(db, scoring_mode="inverse")
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    w1, w2, w3, w4 = _weeks(db, pool, 4)
    _entry(db, pool, w1, alice, points=5, is_winner=True)
    _entry(db, pool, w1, bob, points=50, is_winner=False)
    _entry(db, pool, w2, bob, points=50, is_winner=True)
    _entry(db, pool, w3, bob, points=50, is_winner=True)
    _entry(db, pool, w4, bob, points=50, is_winner=True)

    rows = season_wins_ranking(db, pool)

    assert rows[0].display_name == "Bob"
    assert rows[0].rank == 1
    assert rows[1].display_name == "Alice"


def test_season_ranking_split_mode_restores_the_old_shared_rank_behavior(db):
    pool = _pool(db, season_tiebreak_mode="split")
    alice, bob, carol = _three_players(db, pool)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, is_winner=True)
    _entry(db, pool, week, bob, points=10, is_winner=False)
    _entry(db, pool, week, carol, points=50)

    points_rows = season_points_ranking(db, pool)
    by_name = {r.display_name: r for r in points_rows}
    assert by_name["Alice"].rank == 1
    assert by_name["Bob"].rank == 1
    assert by_name["Carol"].rank == 3
    assert all(r.tiebreak_reason is None for r in points_rows)

    wins_rows = season_wins_ranking(db, pool)
    by_name_w = {r.display_name: r for r in wins_rows}
    assert by_name_w["Alice"].rank == 1
    assert by_name_w["Bob"].rank == 2
    assert by_name_w["Carol"].rank == 2
    assert all(r.tiebreak_reason is None for r in wins_rows)


def test_season_submission_time_orders_a_tied_pair_by_the_final_week(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)  # week_number=1, status="scored": the season's final week too
    early = dt.datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
    late = dt.datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    _entry(db, pool, week, alice, points=10, submitted_at=early)
    _entry(db, pool, week, bob, points=10, submitted_at=late)

    rows = season_points_ranking(db, pool)

    assert [r.display_name for r in rows] == ["Alice", "Bob"]
    assert rows[0].tiebreak_reason == "Tiebreak: submitted week 1 first."
    assert rows[1].tiebreak_reason == "Tiebreak: the other player submitted week 1 first."


def test_season_submission_time_non_submitter_sorts_last(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10)
    _entry(db, pool, week, bob, points=10, did_not_submit=True)

    rows = season_points_ranking(db, pool)

    assert [r.display_name for r in rows] == ["Alice", "Bob"]
    assert rows[1].tiebreak_reason == "Tiebreak: did not submit week 1."


def test_season_submission_time_all_non_submitters_fall_through_to_user_id(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, did_not_submit=True)
    _entry(db, pool, week, bob, points=10, did_not_submit=True)

    rows = season_points_ranking(db, pool)  # must not raise

    assert [r.user_id for r in rows] == sorted(r.user_id for r in rows)
    assert rows[0].tiebreak_reason == "Tiebreak: entry order."


def test_season_ranking_with_zero_scored_weeks_does_not_raise(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)

    points_rows = season_points_ranking(db, pool)
    wins_rows = season_wins_ranking(db, pool)

    assert [r.points for r in points_rows] == [0, 0]
    assert len(wins_rows) == 2
