"""Weekly ties break on total wins entering the week (Phase 3, weekly tiebreak/sorting/
performance work, see PERF-REPORT.md and DECISIONS.md).

Chain: weekly points (pool's own scoring direction), then total wins entering the week
(excluding the week being decided and any test week), then this week's own submission time,
then user id. Mirrors app/services/standings.py's existing season tiebreak chain in shape;
these tests are the weekly equivalent of tests/test_standings.py's season tiebreak coverage.
"""

from __future__ import annotations

import datetime as dt

from app.models import Pool, PoolMember, User, Week, WeekEntry
from app.services.standings import weekly_leaderboard, wins_entering_week

UTC = dt.UTC


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"WKTB{id(overrides) % 100000}",
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


def _member(db, pool: Pool, user: User) -> PoolMember:
    member = PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="member")
    db.add(member)
    db.flush()
    return member


def _week(
    db, pool: Pool, week_number: int, *, status: str = "scored", is_test_week: bool = False
) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=week_number,
        label=f"Week {week_number}",
        status=status,
        is_test_week=is_test_week,
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
    is_winner: bool = False,
    did_not_submit: bool = False,
    submitted_at: dt.datetime | None = None,
) -> WeekEntry:
    entry = WeekEntry(
        pool_id=pool.id,
        week_id=week.id,
        user_id=user.id,
        points=points,
        correct=0,
        possible=14,
        is_winner=is_winner,
        did_not_submit=did_not_submit,
        submitted_at=submitted_at,
    )
    db.add(entry)
    db.flush()
    return entry


def test_more_prior_wins_takes_the_higher_place_on_a_points_tie(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)

    w1 = _week(db, pool, 1)
    w2 = _week(db, pool, 2)
    _entry(db, pool, w1, alice, points=5, is_winner=True)
    _entry(db, pool, w1, bob, points=50, is_winner=False)
    _entry(db, pool, w2, alice, points=10)
    _entry(db, pool, w2, bob, points=10)

    rows, _ = weekly_leaderboard(db, pool, week=w2)

    by_name = {r.display_name: r for r in rows}
    assert by_name["Alice"].rank == 1
    assert by_name["Bob"].rank == 2
    assert by_name["Alice"].tiebreak_reason == "Tiebreak: 1 prior win to 0."
    assert by_name["Bob"].tiebreak_reason == "Tiebreak: 0 prior wins to 1."


def test_prior_wins_exclude_the_week_being_decided(db):
    """Constructed so including week 2's own win would flip the result: Bob wins week 2
    itself (lower points under the pool's default inverse scoring), but entering week 2 both
    have zero prior wins, so the tie falls through to submission time instead of Bob's
    about-to-happen week 2 win deciding it circularly."""
    pool = _pool(db)  # scoring_mode defaults to "inverse": lowest points wins
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)

    week = _week(db, pool, 1)
    early = dt.datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    late = dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    # Tied on points for week 1 itself; Bob would "win" week 1 if it were somehow already
    # scored and fed back into its own tiebreak, but week 1 is the week being decided.
    _entry(db, pool, week, alice, points=10, submitted_at=early)
    _entry(db, pool, week, bob, points=10, submitted_at=late)

    rows, _ = weekly_leaderboard(db, pool, week=week)

    assert wins_entering_week(db, pool, week) == {alice.id: 0, bob.id: 0}
    by_name = {r.display_name: r for r in rows}
    assert by_name["Alice"].rank == 1
    assert by_name["Bob"].rank == 2
    assert by_name["Alice"].tiebreak_reason == "Tiebreak: submitted first."
    assert by_name["Bob"].tiebreak_reason == "Tiebreak: the other player submitted first."


def test_week_1_tie_falls_to_submission_time_without_raising_on_the_all_zero_case(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool, 1)
    early = dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    late = dt.datetime(2026, 9, 1, 9, 30, tzinfo=UTC)
    _entry(db, pool, week, alice, points=10, submitted_at=early)
    _entry(db, pool, week, bob, points=10, submitted_at=late)

    rows, _ = weekly_leaderboard(db, pool, week=week)  # must not raise

    assert [r.display_name for r in rows] == ["Alice", "Bob"]
    assert rows[0].tiebreak_reason == "Tiebreak: submitted first."


def test_three_way_tie_with_three_different_prior_win_counts_orders_all_three(db):
    pool = _pool(db)
    alice, bob, carol = _user(db, "Alice"), _user(db, "Bob"), _user(db, "Carol")
    for u in (alice, bob, carol):
        _member(db, pool, u)

    w1, w2 = (_week(db, pool, n) for n in (1, 2))
    # Alice: 2 prior wins. Bob: 1 prior win. Carol: 0 prior wins.
    _entry(db, pool, w1, alice, points=5, is_winner=True)
    _entry(db, pool, w1, bob, points=50, is_winner=False)
    _entry(db, pool, w1, carol, points=50, is_winner=False)
    _entry(db, pool, w2, alice, points=5, is_winner=True)
    _entry(db, pool, w2, bob, points=5, is_winner=True)
    _entry(db, pool, w2, carol, points=50, is_winner=False)

    week4 = _week(db, pool, 4)
    _entry(db, pool, week4, alice, points=10)
    _entry(db, pool, week4, bob, points=10)
    _entry(db, pool, week4, carol, points=10)

    rows, _ = weekly_leaderboard(db, pool, week=week4)

    assert [r.display_name for r in rows] == ["Alice", "Bob", "Carol"]
    assert [r.rank for r in rows] == [1, 2, 3]


def test_a_test_weeks_win_does_not_count_toward_the_weekly_tiebreak(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    test_week = _week(db, pool, 0, is_test_week=True)
    _entry(db, pool, test_week, bob, points=1, is_winner=True)

    week1 = _week(db, pool, 1)
    early = dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    late = dt.datetime(2026, 9, 1, 9, 30, tzinfo=UTC)
    _entry(db, pool, week1, alice, points=10, submitted_at=early)
    _entry(db, pool, week1, bob, points=10, submitted_at=late)

    counts = wins_entering_week(db, pool, week1)
    assert counts == {alice.id: 0, bob.id: 0}

    rows, _ = weekly_leaderboard(db, pool, week=week1)
    by_name = {r.display_name: r for r in rows}
    # Bob's test week win must not hand him the tie; it falls to submission time, Alice first.
    assert by_name["Alice"].rank == 1
    assert by_name["Alice"].tiebreak_reason == "Tiebreak: submitted first."


def test_weekly_tiebreak_mode_split_restores_the_old_shared_rank_behavior(db):
    pool = _pool(db, weekly_tiebreak_mode="split")
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    w1 = _week(db, pool, 1)
    _entry(db, pool, w1, alice, points=5, is_winner=True)
    _entry(db, pool, w1, bob, points=50, is_winner=False)

    week2 = _week(db, pool, 2)
    _entry(db, pool, week2, alice, points=10)
    _entry(db, pool, week2, bob, points=10)

    rows, _ = weekly_leaderboard(db, pool, week=week2)

    assert rows[0].rank == 1
    assert rows[1].rank == 1  # shared rank: not broken under "split"
    assert all(r.tiebreak_reason is None for r in rows)


def test_inverse_scoring_lowest_points_still_ranks_first_with_tiebreak_active(db):
    """The tiebreak must never invert the pool's own scoring direction: under inverse, the
    LOWEST points is still 1st, the tiebreak only ever decides who is 1st among equals."""
    pool = _pool(db, scoring_mode="inverse")
    alice, bob, carol = _user(db, "Alice"), _user(db, "Bob"), _user(db, "Carol")
    for u in (alice, bob, carol):
        _member(db, pool, u)
    week = _week(db, pool, 1)
    _entry(db, pool, week, alice, points=5)
    _entry(db, pool, week, bob, points=10)
    _entry(db, pool, week, carol, points=10)

    rows, _ = weekly_leaderboard(db, pool, week=week)

    assert rows[0].display_name == "Alice"
    assert rows[0].points == 5
    # Bob and Carol tie on 10; the tiebreak only orders them relative to each other, never
    # ahead of Alice's genuinely lower (better, under inverse) score.
    assert {rows[1].display_name, rows[2].display_name} == {"Bob", "Carol"}
