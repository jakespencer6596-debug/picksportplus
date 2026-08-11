"""Service-level tests for the payout system rebuild, Phase 3: the database-backed layer in
app/services/payouts.py, and its wiring into app/services/results.py.score_week_for_pool.

Most tests build Pool/PoolMember/Week/WeekEntry/PayoutRule rows directly against the `db`
fixture (tests/conftest.py), matching tests/test_payout_models.py's own pattern from Phase 1:
score_week_for_pool is heavier machinery than most of these need. A handful of tests (the ones
about the scoring hook itself) build real Game/Pick rows and call the real score_week_for_pool,
to prove the wiring, not just the service functions in isolation, actually works.
"""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

from app.models import (
    Game,
    PayoutAward,
    PayoutRule,
    Pick,
    Pool,
    PoolMember,
    User,
    Week,
    WeekEntry,
    utcnow,
)
from app.services.payouts import (
    awards_for_week,
    effective_pot,
    mark_paid,
    payout_summary,
    project_awards,
    recalculate_awards,
    snapshot_awards,
)
from app.services.results import score_week_for_pool

UTC = dt.UTC
_join_codes = itertools.count()


# Fixture builders -------------------------------------------------------------


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"PAYSVC{next(_join_codes)}",
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


def _member(db, pool: Pool, user: User, *, paid: bool = False, role: str = "member") -> PoolMember:
    member = PoolMember(
        pool_id=pool.id,
        user_id=user.id,
        role_in_pool=role,
        paid_at=utcnow() if paid else None,
    )
    db.add(member)
    db.flush()
    return member


def _week(
    db, pool: Pool, *, week_number: int = 1, is_bowl_week: bool = False, status: str = "open"
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


def _entry(
    db,
    pool: Pool,
    week: Week,
    user: User,
    *,
    points: int = 0,
    did_not_submit: bool = False,
    is_winner: bool = False,
    submitted_at: dt.datetime | None = None,
    correct: int = 0,
    possible: int = 0,
) -> WeekEntry:
    entry = WeekEntry(
        user_id=user.id,
        pool_id=pool.id,
        week_id=week.id,
        points=points,
        correct=correct,
        possible=possible,
        did_not_submit=did_not_submit,
        is_winner=is_winner,
        submitted_at=submitted_at,
    )
    db.add(entry)
    db.flush()
    return entry


def _rule(
    db, pool: Pool, scope: str, place: int, *, mode: str = "amount", value, label=None
) -> PayoutRule:
    rule = PayoutRule(
        pool_id=pool.id, scope=scope, place=place, mode=mode, value=Decimal(value), label=label
    )
    db.add(rule)
    db.flush()
    return rule


def _games(db, week: Week, count: int = 2) -> list[Game]:
    games = []
    base = dt.datetime.now(UTC) + dt.timedelta(hours=48)
    for i in range(count):
        game = Game(
            week_id=week.id,
            league="nfl",
            espn_event_id=f"evt-{week.id}-{i}",
            start_time=base + dt.timedelta(hours=i),
            home_team=f"Home {i}",
            away_team=f"Away {i}",
            home_abbr=f"H{i}",
            away_abbr=f"A{i}",
            canonical_home_key=f"nfl:home-{week.id}-{i}",
            canonical_away_key=f"nfl:away-{week.id}-{i}",
            spread_home=-1.5,
            closeness=1.5,
            spread_source="espn",
            in_slate=True,
            slate_rank=i + 1,
            status="scheduled",
        )
        db.add(game)
        games.append(game)
    db.flush()
    return games


def _pick(
    db, pool: Pool, week: Week, user: User, game: Game, picked_team: str, confidence: int
) -> Pick:
    pick = Pick(
        user_id=user.id,
        pool_id=pool.id,
        week_id=week.id,
        game_id=game.id,
        picked_team=picked_team,
        confidence=confidence,
    )
    db.add(pick)
    db.flush()
    return pick


def _build_and_score_week(
    db, *, scoring_mode: str = "standard", is_bowl_week: bool = False, with_rule: bool = True
):
    """A minimal but real pool: two members, two slate games, full picks for both, games
    finalised so the winner is unambiguous, then scored through the real score_week_for_pool
    hook. Alice picks both games correctly (points=3 under standard), Bob picks both wrong
    (points=0), so Alice always ranks first regardless of scoring_mode direction quirks.
    Returns (pool, week, alice, bob).
    """
    pool = _pool(db, scoring_mode=scoring_mode, picks_required=2)
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)
    week = _week(db, pool, is_bowl_week=is_bowl_week)
    game0, game1 = _games(db, week, count=2)

    _pick(db, pool, week, alice, game0, "home", 2)
    _pick(db, pool, week, alice, game1, "away", 1)
    _pick(db, pool, week, bob, game0, "away", 2)
    _pick(db, pool, week, bob, game1, "home", 1)

    if with_rule:
        scope = "bowl" if is_bowl_week else "weekly"
        _rule(db, pool, scope, 1, value=Decimal("50"))

    for game in (game0, game1):
        game.status = "final"
    game0.winner = "home"
    game1.winner = "away"
    db.flush()

    score_week_for_pool(db, pool, week)
    db.flush()
    return pool, week, alice, bob


# effective_pot ------------------------------------------------------------------


def test_effective_pot_from_entry_fee_and_paid_members(db):
    pool = _pool(db, entry_fee=Decimal("25.00"))
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    carol = _user(db, "Carol")
    _member(db, pool, alice, paid=True)
    _member(db, pool, bob, paid=True)
    _member(db, pool, carol, paid=False)

    assert effective_pot(db, pool) == Decimal("50.00")


def test_effective_pot_prefers_override_even_when_entry_fee_and_paid_members_are_set(db):
    pool = _pool(db, entry_fee=Decimal("25.00"), pot_override=Decimal("999.00"))
    alice = _user(db, "Alice")
    _member(db, pool, alice, paid=True)

    assert effective_pot(db, pool) == Decimal("999.00")


def test_effective_pot_is_zero_with_neither_entry_fee_nor_override(db):
    pool = _pool(db)
    assert effective_pot(db, pool) == Decimal("0")


def test_effective_pot_is_zero_with_entry_fee_but_no_paid_members(db):
    pool = _pool(db, entry_fee=Decimal("25.00"))
    alice = _user(db, "Alice")
    _member(db, pool, alice, paid=False)

    assert effective_pot(db, pool) == Decimal("0")


# The real scoring hook ------------------------------------------------------------


def test_scoring_a_week_auto_creates_weekly_payout_awards(db):
    pool, week, alice, bob = _build_and_score_week(db)

    awards = list(db.query(PayoutAward).filter(PayoutAward.pool_id == pool.id))
    assert len(awards) == 1
    assert awards[0].scope == "weekly"
    assert awards[0].week_id == week.id
    assert awards[0].user_id == alice.id
    assert awards[0].amount == Decimal("50.00")


def test_rerunning_score_week_for_pool_creates_no_duplicates_and_no_amount_drift(db):
    pool, week, alice, bob = _build_and_score_week(db)

    awards_after_first = list(db.query(PayoutAward).filter(PayoutAward.pool_id == pool.id))
    assert len(awards_after_first) == 1
    amount_after_first = awards_after_first[0].amount

    score_week_for_pool(db, pool, week)
    db.flush()
    score_week_for_pool(db, pool, week)
    db.flush()

    awards_after = list(db.query(PayoutAward).filter(PayoutAward.pool_id == pool.id))
    assert len(awards_after) == 1
    assert awards_after[0].amount == amount_after_first == Decimal("50.00")


def test_bowl_week_snapshots_under_bowl_scope_not_weekly(db):
    pool, week, alice, bob = _build_and_score_week(db, is_bowl_week=True)

    awards = awards_for_week(db, pool, week)
    assert len(awards) == 1
    assert awards[0].scope == "bowl"
    assert awards[0].user_id == alice.id

    weekly_only = [a for a in awards if a.scope == "weekly"]
    assert weekly_only == []


def test_pool_with_zero_payout_rules_scores_without_raising(db):
    pool, week, alice, bob = _build_and_score_week(db, with_rule=False)

    awards = list(db.query(PayoutAward).filter(PayoutAward.pool_id == pool.id))
    assert awards == []


# The headline test: frozen amounts survive the pot growing later ------------------


def test_snapshot_amounts_survive_the_pot_growing_after_the_fact(db):
    pool = _pool(db, entry_fee=Decimal("20.00"), payout_rounding="dollar", scoring_mode="standard")
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    carol = _user(db, "Carol")
    _member(db, pool, alice, paid=True)
    _member(db, pool, bob, paid=True)
    _member(db, pool, carol, paid=True)  # 3 paid members: pot = 60

    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10)  # ranks first under "standard"
    _entry(db, pool, week, bob, points=5)

    _rule(db, pool, "weekly", 1, mode="percent", value=Decimal("50"))
    _rule(db, pool, "weekly", 2, mode="percent", value=Decimal("30"))

    assert effective_pot(db, pool) == Decimal("60.00")
    snapshot_awards(db, pool, "weekly", week=week)

    frozen = {
        a.user_id: a
        for a in db.query(PayoutAward).filter(
            PayoutAward.pool_id == pool.id, PayoutAward.scope == "weekly"
        )
    }
    assert frozen[alice.id].amount == Decimal("30.00")  # 50% of 60
    assert frozen[bob.id].amount == Decimal("18.00")  # 30% of 60
    assert frozen[alice.id].pot_at_award == Decimal("60.00")

    # Two more members pay up after the week is already scored: the pot grows.
    dave = _user(db, "Dave")
    erin = _user(db, "Erin")
    _member(db, pool, dave, paid=True)
    _member(db, pool, erin, paid=True)
    assert effective_pot(db, pool) == Decimal("100.00")

    live_awards = {a.user_id: a for a in project_awards(db, pool, "weekly", week=week)}
    assert live_awards[alice.id].amount == Decimal("50.00")  # 50% of the now-grown 100
    assert live_awards[bob.id].amount == Decimal("30.00")  # 30% of 100
    assert live_awards[alice.id].amount != frozen[alice.id].amount

    reread = {
        a.user_id: a
        for a in db.query(PayoutAward).filter(
            PayoutAward.pool_id == pool.id, PayoutAward.scope == "weekly"
        )
    }
    assert reread[alice.id].amount == Decimal("30.00")
    assert reread[bob.id].amount == Decimal("18.00")
    assert reread[alice.id].pot_at_award == Decimal("60.00")


# recalculate_awards and mark_paid -------------------------------------------------


def test_recalculate_awards_overwrites_amounts_and_preserves_paid_at(db):
    pool = _pool(db, scoring_mode="standard")
    commissioner = _user(db, "Commissioner")
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, commissioner, role="commissioner")
    _member(db, pool, alice)
    _member(db, pool, bob)

    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10)
    _entry(db, pool, week, bob, points=5)
    rule = _rule(db, pool, "weekly", 1, value=Decimal("50"))

    [award] = snapshot_awards(db, pool, "weekly", week=week)
    assert award.amount == Decimal("50.00")

    paid = mark_paid(db, award.id, commissioner)
    original_paid_at = paid.paid_at
    assert original_paid_at is not None

    rule.value = Decimal("80")
    db.flush()

    [recalculated] = recalculate_awards(db, pool, "weekly", week, commissioner)
    assert recalculated.id == award.id
    assert recalculated.amount == Decimal("80.00")
    assert recalculated.recalculated_at is not None
    assert recalculated.recalculated_by_user_id == commissioner.id
    assert recalculated.paid_at == original_paid_at
    assert recalculated.paid_marked_by_user_id == commissioner.id


def test_mark_paid_is_idempotent_on_the_timestamp(db):
    pool = _pool(db, scoring_mode="standard")
    commissioner = _user(db, "Commissioner")
    alice = _user(db, "Alice")
    _member(db, pool, commissioner, role="commissioner")
    _member(db, pool, alice)

    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10)
    _rule(db, pool, "weekly", 1, value=Decimal("50"))

    [award] = snapshot_awards(db, pool, "weekly", week=week)

    first = mark_paid(db, award.id, commissioner)
    first_paid_at = first.paid_at
    assert first_paid_at is not None

    second = mark_paid(db, award.id, commissioner)
    assert second.paid_at == first_paid_at
    assert second.paid_marked_by_user_id == commissioner.id


# No-show exclusion ----------------------------------------------------------------


def test_no_show_is_excluded_from_weekly_awards_even_if_their_points_would_have_placed(db):
    # Under scoring_mode "inverse" (lowest total wins), a no-show with WeekEntry.points=0
    # would rank first if it were counted at all: a no-show must never win money regardless
    # of what raw number happens to sit in their points column.
    pool = _pool(db, scoring_mode="inverse")
    noshow = _user(db, "Noshow")
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, noshow)
    _member(db, pool, alice)
    _member(db, pool, bob)

    week = _week(db, pool)
    _entry(db, pool, week, noshow, points=0, did_not_submit=True)
    _entry(db, pool, week, alice, points=5, did_not_submit=False)
    _entry(db, pool, week, bob, points=10, did_not_submit=False)

    _rule(db, pool, "weekly", 1, value=Decimal("100"))

    awards = snapshot_awards(db, pool, "weekly", week=week)
    winners = {a.user_id for a in awards}
    assert noshow.id not in winners
    assert winners == {alice.id}
    assert awards[0].amount == Decimal("100.00")


# season_wins versus season_points --------------------------------------------------


def test_season_wins_ranks_by_weekly_wins_independent_of_season_points(db):
    pool = _pool(db, scoring_mode="standard")
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)

    week = _week(db, pool)
    # Alice has more season points but never actually won a week; Bob has fewer points but
    # took the one week that mattered.
    _entry(db, pool, week, alice, points=10, is_winner=False)
    _entry(db, pool, week, bob, points=5, is_winner=True)

    _rule(db, pool, "season_points", 1, value=Decimal("100"))
    _rule(db, pool, "season_wins", 1, value=Decimal("60"))

    points_awards = project_awards(db, pool, "season_points")
    wins_awards = project_awards(db, pool, "season_wins")

    assert [a.user_id for a in points_awards] == [alice.id]
    assert [a.user_id for a in wins_awards] == [bob.id]


# payout_summary -----------------------------------------------------------------


def test_payout_summary_totals_across_scopes_and_reconciles_paid_and_unpaid(db):
    pool = _pool(db, scoring_mode="standard")
    # The commissioner acts as mark_paid's actor but is deliberately not made a PoolMember
    # here: adding them as a member with no WeekEntry would tie them with Bob at
    # weekly_wins=0 in the season_wins scope, splitting that place between the two of them
    # and making Bob's totals depend on an incidental tie rather than the scenario this test
    # means to exercise.
    commissioner = _user(db, "Commissioner")
    alice = _user(db, "Alice")
    bob = _user(db, "Bob")
    _member(db, pool, alice)
    _member(db, pool, bob)

    week = _week(db, pool)
    _entry(db, pool, week, alice, points=10, correct=2, possible=2, is_winner=True)
    _entry(db, pool, week, bob, points=5, correct=1, possible=2, is_winner=False)

    _rule(db, pool, "weekly", 1, value=Decimal("50"))
    _rule(db, pool, "weekly", 2, value=Decimal("20"))
    _rule(db, pool, "season_points", 1, value=Decimal("100"))
    _rule(db, pool, "season_points", 2, value=Decimal("40"))
    _rule(db, pool, "season_wins", 1, value=Decimal("60"))
    _rule(db, pool, "season_wins", 2, value=Decimal("10"))

    snapshot_awards(db, pool, "weekly", week=week)
    snapshot_awards(db, pool, "season_points", week=None)
    snapshot_awards(db, pool, "season_wins", week=None)

    rows = payout_summary(db, pool)
    by_user = {row.user_id: row for row in rows}

    alice_row = by_user[alice.id]
    assert alice_row.weekly_total == Decimal("50.00")
    assert alice_row.season_points_total == Decimal("100.00")
    assert alice_row.season_wins_total == Decimal("60.00")
    assert alice_row.grand_total == Decimal("210.00")
    assert alice_row.paid_total == Decimal("0")
    assert alice_row.unpaid_total == Decimal("210.00")

    bob_row = by_user[bob.id]
    assert bob_row.grand_total == Decimal("70.00")

    # Rows sorted by grand_total descending.
    assert rows[0].user_id == alice.id

    weekly_award = alice_row.awards_by_scope["weekly"][0]
    mark_paid(db, weekly_award.id, commissioner)

    rows_after = payout_summary(db, pool)
    alice_after = {row.user_id: row for row in rows_after}[alice.id]
    assert alice_after.paid_total == Decimal("50.00")
    assert alice_after.unpaid_total == Decimal("160.00")
    assert alice_after.grand_total == Decimal("210.00")
    assert alice_after.paid_total + alice_after.unpaid_total == alice_after.grand_total

    bob_after = {row.user_id: row for row in rows_after}[bob.id]
    assert bob_after.paid_total == Decimal("0")
    assert bob_after.unpaid_total == bob_after.grand_total
