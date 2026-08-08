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
from app.services.standings import season_standings, weekly_leaderboard

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
) -> WeekEntry:
    entry = WeekEntry(
        pool_id=pool.id,
        week_id=week.id,
        user_id=user.id,
        points=points,
        correct=correct,
        possible=possible,
        is_winner=is_winner,
        did_not_submit=did_not_submit,
        submitted_at=None if did_not_submit else dt.datetime.now(UTC),
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
