"""Tests for app/services/payouts.py (Phase 7).

allocate_payouts is pure arithmetic, no database: (user_id, rank) pairs plus a place-to-
amount mapping in, a user_id-to-dollars mapping out. Every tie split test below writes the
cent arithmetic out explicitly and asserts the split sums back to the combined total exactly,
per the brief, rather than pinning a magic number nobody can check by eye.

week_payout_scope and week_is_complete take plain model instances that are never flushed to a
database (constructed with exactly the attributes each function reads), since neither
function needs anything else about a Week or Game to answer its one question.
"""

from __future__ import annotations

import datetime as dt

from app.models import Game, PayoutRule, Pool, PoolMember, User, Week, WeekEntry
from app.services.payouts import (
    allocate_payouts,
    payout_summary,
    rules_by_place,
    season_payouts,
    week_is_complete,
    week_payout_scope,
    weekly_payouts,
)
from app.services.standings import season_standings, weekly_leaderboard

UTC = dt.UTC


# allocate_payouts: the pure tie-split arithmetic ----------------------------


def test_allocate_payouts_clean_1_2_3_4_no_ties():
    ranked = [(101, 1), (102, 2), (103, 3), (104, 4)]
    amounts = {1: 100.0, 2: 50.0, 3: 25.0, 4: 10.0}

    result = allocate_payouts(ranked, amounts)

    assert result == {101: 100.0, 102: 50.0, 103: 25.0, 104: 10.0}


def test_allocate_payouts_no_rules_at_all_returns_empty():
    assert allocate_payouts([(1, 1), (2, 2)], {}) == {}
    assert allocate_payouts([], {1: 100.0}) == {}


def test_allocate_payouts_omits_a_rank_past_every_configured_place():
    # Only places 1 and 2 are configured. The rank 3 finisher gets nothing at all, not a
    # $0.00 entry, since this pool wrote no rule for 3rd.
    ranked = [(1, 1), (2, 2), (3, 3)]
    amounts = {1: 20.0, 2: 10.0}

    result = allocate_payouts(ranked, amounts)

    assert result == {1: 20.0, 2: 10.0}
    assert 3 not in result


def test_allocate_payouts_two_way_tie_splits_evenly_with_remainder_cent_to_earliest():
    # Alice and Bob tie for 1st (rank 1 each), Carol is 3rd alone. Alice submitted first, so
    # she is listed first in the tie-break order allocate_payouts is handed.
    ranked = [(1, 1), (2, 1), (3, 3)]
    # Places 1 and 2 (what a two way tie for 1st occupies) pay 100.01 and 50.00: combined
    # 150.01, an odd number of cents (15001) that does not split evenly in two.
    amounts = {1: 100.01, 2: 50.00, 3: 25.0}

    result = allocate_payouts(ranked, amounts)

    # 15001 cents / 2 = 7500 remainder 1. Alice (listed first, the earlier submitter) gets
    # the extra cent: 7501 cents = $75.01. Bob gets the even share: 7500 cents = $75.00.
    assert result[1] == 75.01
    assert result[2] == 75.00
    assert result[3] == 25.0
    # The split reconciles to the combined amount exactly, to the cent.
    assert round(result[1] + result[2], 2) == round(100.01 + 50.00, 2)


def test_allocate_payouts_three_way_tie_splits_evenly_with_remainder_cent_to_earliest():
    # Alice, Bob and Carol tie for 1st (rank 1 each, listed earliest submitter first), Dave
    # is 4th alone. The tied trio occupies places 1, 2 and 3.
    ranked = [(1, 1), (2, 1), (3, 1), (4, 4)]
    amounts = {1: 100.0, 2: 50.0, 3: 33.34, 4: 10.0}

    result = allocate_payouts(ranked, amounts)

    # Combined cents for places 1-3: 10000 + 5000 + 3334 = 18334. 18334 // 3 = 6111,
    # remainder 1 (6111 * 3 = 18333, one cent left over). Alice gets the extra cent.
    combined_cents = round(100.0 * 100) + round(50.0 * 100) + round(33.34 * 100)
    assert combined_cents == 18334
    share_cents, remainder_cents = divmod(combined_cents, 3)
    assert (share_cents, remainder_cents) == (6111, 1)

    assert result[1] == (share_cents + 1) / 100  # 61.12, the earliest submitter
    assert result[2] == share_cents / 100  # 61.11
    assert result[3] == share_cents / 100  # 61.11
    assert result[4] == 10.0

    # The three way split reconciles to the combined amount exactly.
    assert round(result[1] + result[2] + result[3], 2) == round(100.0 + 50.0 + 33.34, 2)


def test_allocate_payouts_tied_group_straddling_an_unconfigured_place():
    # A two way tie for 4th occupies places 4 and 5, but only place 4 has a rule. Place 5's
    # missing rule counts as $0 toward the combined amount, per allocate_payouts' own
    # sparse-mapping contract.
    ranked = [(1, 4), (2, 4)]
    amounts = {1: 100.0, 2: 50.0, 3: 25.0, 4: 20.0}

    result = allocate_payouts(ranked, amounts)

    assert result == {1: 10.0, 2: 10.0}


def test_allocate_payouts_omits_a_zero_share():
    # A place explicitly worth $0 pays nobody, rather than a $0.00 row.
    ranked = [(1, 1)]
    result = allocate_payouts(ranked, {1: 0.0})
    assert result == {}


# week_payout_scope and week_is_complete -------------------------------------


def test_week_payout_scope_routes_bowl_week_to_bowl():
    week = Week(is_bowl_week=True)
    assert week_payout_scope(week) == "bowl"


def test_week_payout_scope_routes_ordinary_week_to_weekly():
    week = Week(is_bowl_week=False)
    assert week_payout_scope(week) == "weekly"


def test_week_is_complete_true_once_nothing_is_scheduled_or_in_progress():
    games = [Game(status="final"), Game(status="void"), Game(status="final")]
    assert week_is_complete(games) is True


def test_week_is_complete_false_while_a_game_is_still_playable():
    games = [Game(status="final"), Game(status="in_progress")]
    assert week_is_complete(games) is False

    games = [Game(status="scheduled")]
    assert week_is_complete(games) is False


def test_week_is_complete_false_for_an_empty_slate():
    assert week_is_complete([]) is False


# Database backed helpers -----------------------------------------------------


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"PAYCODE{id(overrides) % 100000}",
        "season_year": 2026,
        "sports": ["nfl", "ncaaf"],
        "timezone": "America/New_York",
        "current_week": 1,
        "scoring_mode": "inverse",
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
    db, pool: Pool, week_number: int = 1, status: str = "scored", is_bowl_week: bool = False
) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=week_number,
        label=f"Week {week_number}",
        status=status,
        is_bowl_week=is_bowl_week,
    )
    db.add(week)
    db.flush()
    return week


def _entry(db, pool: Pool, week: Week, user: User, *, points: int, submitted_at) -> WeekEntry:
    entry = WeekEntry(
        pool_id=pool.id,
        week_id=week.id,
        user_id=user.id,
        points=points,
        correct=0,
        possible=0,
        submitted_at=submitted_at,
    )
    db.add(entry)
    db.flush()
    return entry


def _rule(db, pool: Pool, scope: str, place: int, amount: float) -> PayoutRule:
    rule = PayoutRule(pool_id=pool.id, scope=scope, place=place, amount=amount)
    db.add(rule)
    db.flush()
    return rule


def test_rules_by_place_reads_only_the_requested_scope(db):
    pool = _pool(db)
    _rule(db, pool, "weekly", 1, 100.0)
    _rule(db, pool, "bowl", 1, 200.0)
    _rule(db, pool, "season", 1, 300.0)

    assert rules_by_place(db, pool, "weekly") == {1: 100.0}
    assert rules_by_place(db, pool, "bowl") == {1: 200.0}
    assert rules_by_place(db, pool, "season") == {1: 300.0}


def test_rules_by_place_empty_scope_returns_empty_dict(db):
    pool = _pool(db)
    assert rules_by_place(db, pool, "weekly") == {}


def test_weekly_payouts_ties_broken_by_earliest_submitted_at(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    # Same inverse score for both (a tie for 1st), Alice submitted first.
    _entry(db, pool, week, alice, points=5, submitted_at=dt.datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    _entry(db, pool, week, bob, points=5, submitted_at=dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

    rows, _ = weekly_leaderboard(db, pool, week=week)
    rules = {1: 100.01, 2: 50.0}

    result = weekly_payouts(db, week, rows, rules)

    # Combined for the tied pair occupying places 1 and 2: 15001 cents, 7501/7500 split.
    assert result[alice.id] == 75.01
    assert result[bob.id] == 75.00


def test_weekly_payouts_no_rules_returns_empty(db):
    pool = _pool(db)
    alice = _user(db, "Alice")
    _member(db, pool, alice)
    week = _week(db, pool)
    _entry(db, pool, week, alice, points=5, submitted_at=dt.datetime.now(UTC))
    rows, _ = weekly_leaderboard(db, pool, week=week)

    assert weekly_payouts(db, week, rows, {}) == {}


def test_bowl_week_routes_to_bowl_rules_not_weekly_even_when_both_exist(db):
    """The brief's own named test case: a bowl week's payout must come from scope="bowl"
    rules, never scope="weekly", even though weekly rules also exist for this pool."""
    pool = _pool(db)
    alice = _user(db, "Alice")
    _member(db, pool, alice)
    bowl_week = _week(db, pool, week_number=17, is_bowl_week=True)
    _entry(db, pool, bowl_week, alice, points=0, submitted_at=dt.datetime.now(UTC))
    rows, _ = weekly_leaderboard(db, pool, week=bowl_week)

    _rule(db, pool, "weekly", 1, 999.0)
    _rule(db, pool, "bowl", 1, 55.0)
    weekly_rules = rules_by_place(db, pool, "weekly")
    bowl_rules = rules_by_place(db, pool, "bowl")

    scope = week_payout_scope(bowl_week)
    assert scope == "bowl"
    rules = bowl_rules if scope == "bowl" else weekly_rules
    result = weekly_payouts(db, bowl_week, rows, rules)

    assert result == {alice.id: 55.0}


def test_season_payouts_uses_season_standings_order_for_the_tie_break(db):
    pool = _pool(db)
    alice, bob = _user(db, "Alice"), _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool)
    # Both tie for 1st in the season standings (same points, same correct, same weekly
    # wins); season_standings' own final tiebreak is display name, so Alice sorts first.
    _entry(db, pool, week, alice, points=3, submitted_at=dt.datetime.now(UTC))
    _entry(db, pool, week, bob, points=3, submitted_at=dt.datetime.now(UTC))

    rows = season_standings(db, pool)
    result = season_payouts(rows, {1: 10.01, 2: 5.0})

    assert result[alice.id] == 7.51
    assert result[bob.id] == 7.50


def test_payout_summary_totals_weekly_bowl_and_season_per_player(db):
    pool = _pool(db)
    alice = _user(db, "Alice")
    _member(db, pool, alice)

    ordinary_week = _week(db, pool, week_number=1, status="scored", is_bowl_week=False)
    bowl_week = _week(db, pool, week_number=18, status="scored", is_bowl_week=True)
    now = dt.datetime.now(UTC)
    _entry(db, pool, ordinary_week, alice, points=0, submitted_at=now)
    _entry(db, pool, bowl_week, alice, points=0, submitted_at=now)
    # A game on each week, both final, so week_is_complete is true for both.
    db.add(
        Game(
            week_id=ordinary_week.id,
            league="nfl",
            espn_event_id="w1",
            start_time=now,
            home_team="H",
            away_team="A",
            home_abbr="H",
            away_abbr="A",
            canonical_home_key="nfl:h",
            canonical_away_key="nfl:a",
            in_slate=True,
            status="final",
        )
    )
    db.add(
        Game(
            week_id=bowl_week.id,
            league="nfl",
            espn_event_id="w18",
            start_time=now,
            home_team="H",
            away_team="A",
            home_abbr="H",
            away_abbr="A",
            canonical_home_key="nfl:h",
            canonical_away_key="nfl:a",
            in_slate=True,
            status="final",
        )
    )
    _rule(db, pool, "weekly", 1, 40.0)
    _rule(db, pool, "bowl", 1, 60.0)
    _rule(db, pool, "season", 1, 80.0)
    db.commit()

    summary = payout_summary(db, pool)

    assert len(summary) == 1
    row = summary[0]
    assert row.display_name == "Alice"
    assert row.weekly_total == 40.0
    assert row.bowl_total == 60.0
    assert row.season_award == 80.0
    assert row.total_owed == 180.0
