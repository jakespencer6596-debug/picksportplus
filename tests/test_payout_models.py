"""Model-level tests for the payout system rebuild (PayoutRule, PayoutAward, and the new
Pool payout columns). Pure ORM/constraint behavior, no routes, no services: app/models.py's
own contract, exercised directly against a throwaway SQLite session (the `db` fixture in
conftest.py). See DECISIONS.md, "Payout system", Phase 0, for why this replaces the old
float, three-scope payout_rules shape outright rather than migrating old rows into it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import PayoutAward, PayoutRule, Pool, User, Week

UTC = dt.UTC


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


# Model instantiation and defaults --------------------------------------------


def test_pool_payout_columns_have_the_documented_defaults(db):
    pool = _pool(db)
    assert pool.entry_fee is None
    assert pool.pot_override is None
    assert pool.weekly_payout_weeks == 15
    assert pool.payout_rounding == "dollar"
    assert pool.payout_tiebreak == "earliest_submit"


def test_payout_rule_instantiates_with_decimal_value(db):
    pool = _pool(db)
    rule = PayoutRule(
        pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal("105.00")
    )
    db.add(rule)
    db.flush()
    assert rule.id is not None
    assert isinstance(rule.value, Decimal)
    assert rule.mode == "amount"
    assert rule.created_at is not None
    assert rule.updated_at is not None


def test_payout_award_instantiates_with_decimal_amounts(db):
    pool = _pool(db)
    user = _user(db, "Alice")
    award = PayoutAward(
        pool_id=pool.id,
        user_id=user.id,
        scope="season_points",
        week_id=None,
        place=1,
        tied_with=1,
        amount=Decimal("600.00"),
        pot_at_award=Decimal("4950.00"),
        rule_mode="amount",
        rule_value=Decimal("600.0000"),
        awarded_at=dt.datetime.now(UTC),
    )
    db.add(award)
    db.flush()
    assert award.id is not None
    assert isinstance(award.amount, Decimal)
    assert isinstance(award.pot_at_award, Decimal)
    assert award.paid_at is None


# Unique constraints -----------------------------------------------------------


def test_duplicate_pool_scope_place_rejected(db):
    pool = _pool(db)
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal(100)))
    db.flush()
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal(50)))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_same_place_in_a_different_scope_is_allowed(db):
    pool = _pool(db)
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal(100)))
    db.add(PayoutRule(pool_id=pool.id, scope="bowl", place=1, mode="amount", value=Decimal(250)))
    db.flush()  # does not raise
    rows = list(db.query(PayoutRule).all())
    assert len(rows) == 2


def test_duplicate_award_pool_scope_week_user_rejected(db):
    # week_id must be a real (non-null) value for this constraint to bite: standard SQL
    # unique constraints treat NULL as distinct from NULL, so two season-scope awards (which
    # always carry week_id=None) are never caught by this constraint alone. The weekly/bowl
    # case below is the one the database itself enforces; the service layer (Phase 3) is
    # responsible for season scope idempotency by querying before writing, not by relying on
    # this index. See the note in test_null_week_id_is_not_covered_by_the_unique_constraint.
    pool = _pool(db)
    user = _user(db, "Alice")
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=1,
        label="Week 1",
        status="scored",
    )
    db.add(week)
    db.flush()
    kwargs = {
        "pool_id": pool.id,
        "user_id": user.id,
        "scope": "weekly",
        "week_id": week.id,
        "place": 1,
        "tied_with": 1,
        "amount": Decimal("10.00"),
        "pot_at_award": Decimal("100.00"),
        "rule_mode": "amount",
        "rule_value": Decimal("10.0000"),
        "awarded_at": dt.datetime.now(UTC),
    }
    db.add(PayoutAward(**kwargs))
    db.flush()
    db.add(PayoutAward(**kwargs))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_null_week_id_is_not_covered_by_the_unique_constraint(db):
    """Documents a real SQL semantics gap rather than hiding it: two season-scope awards
    (week_id always None) for the same user do not collide at the database level, since NULL
    is never equal to NULL in a unique index. Season scope idempotency is the service layer's
    job (query-before-write in app/services/payouts.py.snapshot_awards), not this
    constraint's. See DECISIONS.md, "Payout system"."""
    pool = _pool(db)
    user = _user(db, "Alice")
    kwargs = {
        "pool_id": pool.id,
        "user_id": user.id,
        "scope": "season_points",
        "week_id": None,
        "place": 1,
        "tied_with": 1,
        "amount": Decimal("600.00"),
        "pot_at_award": Decimal("4950.00"),
        "rule_mode": "amount",
        "rule_value": Decimal("600.0000"),
        "awarded_at": dt.datetime.now(UTC),
    }
    db.add(PayoutAward(**kwargs))
    db.flush()
    db.add(PayoutAward(**kwargs))
    db.flush()  # does not raise: this is the documented gap, not a bug
    assert db.query(PayoutAward).count() == 2


# Check constraints --------------------------------------------------------------


def test_place_zero_rejected(db):
    pool = _pool(db)
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=0, mode="amount", value=Decimal(10)))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_negative_value_rejected(db):
    pool = _pool(db)
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal(-5)))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_place_with_no_upper_bound_is_allowed(db):
    pool = _pool(db)
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=25, mode="amount", value=Decimal(1)))
    db.flush()  # does not raise, PayoutRule.place has no upper bound


# Cascade delete -----------------------------------------------------------------


def test_deleting_a_pool_cascades_to_its_payout_rules_and_awards(db):
    pool = _pool(db)
    user = _user(db, "Alice")
    db.add(PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal(100)))
    db.add(
        PayoutAward(
            pool_id=pool.id,
            user_id=user.id,
            scope="weekly",
            week_id=None,
            place=1,
            tied_with=1,
            amount=Decimal("100.00"),
            pot_at_award=Decimal("100.00"),
            rule_mode="amount",
            rule_value=Decimal("100.0000"),
            awarded_at=dt.datetime.now(UTC),
        )
    )
    db.flush()
    assert db.query(PayoutRule).count() == 1
    assert db.query(PayoutAward).count() == 1

    db.delete(pool)
    db.flush()

    assert db.query(PayoutRule).count() == 0
    assert db.query(PayoutAward).count() == 0
