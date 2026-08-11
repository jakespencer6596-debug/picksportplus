"""Tests for app/cli.py's seed_admin.

Phase 5 flipped Pool.auto_publish's model default to False and removed the hard coded
auto_publish=True from seed_admin's Pool(...) call (app/cli.py). Without that second fix, a
freshly seeded pool would have kept silently auto publishing forever, model default or not.
This is the one CLI command tested in this repo directly (through app.db.session_scope,
pointed at a throwaway database), specifically to prove that end to end rather than only at
the model layer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import typer
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import (
    Base,
    PayoutAward,
    PayoutRule,
    Pool,
    PoolMember,
    User,
    Week,
    WeekEntry,
    utcnow,
)


@pytest.fixture
def isolated_db(monkeypatch):
    """Point app.db's session factory at a throwaway in-memory database.

    seed_admin always goes through app.db.session_scope, which reads app.db.SessionLocal at
    call time, so patching that one name is enough to keep this test from ever touching the
    real configured database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    monkeypatch.setattr("app.db.SessionLocal", session_factory)
    try:
        yield session_factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _configure_admin_credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_email", "commissioner@example.com")
    monkeypatch.setattr(settings, "admin_password", "hunter2hunter2")
    monkeypatch.setattr(settings, "default_join_code", "TESTCODE")


def test_seed_admin_creates_a_pool_with_auto_publish_off(isolated_db, monkeypatch):
    _configure_admin_credentials(monkeypatch)

    from app.cli import seed_admin

    seed_admin()

    session = isolated_db()
    try:
        pool = session.scalar(select(Pool))
        assert pool is not None
        assert pool.auto_publish is False
        user = session.scalar(select(User).where(User.email == "commissioner@example.com"))
        assert user is not None
        assert user.is_admin
    finally:
        session.close()


def test_seed_admin_leaves_the_admin_with_zero_pool_memberships(isolated_db, monkeypatch):
    """Post-launch fix: the site admin can never hold a PoolMember row, structurally, not
    just by convention. seed_admin still creates the starter pool exactly as before, it just
    no longer enrolls the admin into it; the pool waits for a real commissioner to be attached
    from the admin portal."""
    _configure_admin_credentials(monkeypatch)

    from app.cli import seed_admin

    seed_admin()

    session = isolated_db()
    try:
        pool = session.scalar(select(Pool))
        assert pool is not None
        user = session.scalar(select(User).where(User.email == "commissioner@example.com"))
        assert user is not None
        assert user.is_admin
        memberships = session.scalars(select(PoolMember).where(PoolMember.user_id == user.id)).all()
        assert memberships == []
    finally:
        session.close()


def test_seed_admin_is_idempotent_and_leaves_auto_publish_alone_on_a_rerun(
    isolated_db, monkeypatch
):
    # A commissioner who has since turned auto_publish back on for their real pool must not
    # have that choice silently reset every time the app boots (seed-admin runs on every
    # boot in production, see app/cli.py's own docstring).
    _configure_admin_credentials(monkeypatch)

    from app.cli import seed_admin

    seed_admin()
    session = isolated_db()
    try:
        pool = session.scalar(select(Pool))
        pool.auto_publish = True
        session.commit()
    finally:
        session.close()

    seed_admin()

    session = isolated_db()
    try:
        pool = session.scalar(select(Pool))
        assert pool.auto_publish is True
    finally:
        session.close()


def test_ensure_preview_pool_creates_a_hidden_pool_with_seed_admin_defaults(isolated_db):
    """seed-preview's own build step needs the network (ESPN), which this offline test suite
    never touches (tests/conftest.py's force_offline_mode). ensure_preview_pool itself is
    pure database work, so it is exercised directly here rather than through the full CLI
    command."""
    from app.services.preview import ensure_preview_pool, get_preview_pool

    session = isolated_db()
    try:
        assert get_preview_pool(session) is None

        pool = ensure_preview_pool(session)
        session.commit()

        assert pool.is_preview is True
        assert pool.num_games_per_week == 20
        assert pool.target_nfl == 8
        assert pool.target_ncaaf == 12
        memberships = session.scalars(select(PoolMember).where(PoolMember.pool_id == pool.id)).all()
        assert memberships == []
    finally:
        session.close()


def test_ensure_preview_pool_is_idempotent(isolated_db):
    from app.services.preview import ensure_preview_pool

    session = isolated_db()
    try:
        first = ensure_preview_pool(session)
        session.commit()
        first_id = first.id

        again = ensure_preview_pool(session)
        session.commit()

        assert again.id == first_id
        assert session.scalar(select(Pool).where(Pool.is_preview.is_(True))) is not None
        assert len(list(session.scalars(select(Pool).where(Pool.is_preview.is_(True))))) == 1
    finally:
        session.close()


# Payout CLI commands (Payout system rebuild, Phase 7) -----------------------------------
#
# These commands, like every other command tested above, go through app.db.session_scope,
# which reads app.db.SessionLocal at call time, so the isolated_db fixture is what keeps
# them off the real configured database. Every payouts-* command is called directly as a
# plain Python function here (not through Typer's CLI parser), so every optional argument is
# passed explicitly: a caller relying on a typer.Option(...) sentinel's own "default" would
# not get the CLI's resolved default, it would get the OptionInfo object itself.


def _seed_payout_pool(session_factory) -> tuple[int, int, int, int]:
    """A pool with two paid members, one already-scored week (WeekEntry rows only, no real
    Game/Pick rows, matching test_payout_service.py's own lighter weight fixture style) and a
    single weekly place-1 rule worth $50. Alice outscores Bob under "standard" scoring, so
    Alice is the only one who places. Commits and closes its own session, so the CLI command
    under test opens a clean one of its own, exactly as it would against a real database.
    Returns (pool_id, week_id, alice_id, bob_id).
    """
    session = session_factory()
    try:
        pool = Pool(
            name="Payout CLI Pool",
            join_code="PAYCLI1",
            season_year=2026,
            sports=["nfl", "ncaaf"],
            timezone="America/New_York",
            current_week=1,
            scoring_mode="standard",
        )
        session.add(pool)
        session.flush()

        alice = User(email="alice@example.com", password_hash="x", display_name="Alice")
        bob = User(email="bob@example.com", password_hash="x", display_name="Bob")
        session.add_all([alice, bob])
        session.flush()

        session.add(PoolMember(pool_id=pool.id, user_id=alice.id, paid_at=utcnow()))
        session.add(PoolMember(pool_id=pool.id, user_id=bob.id, paid_at=utcnow()))

        week = Week(
            pool_id=pool.id,
            season_year=pool.season_year,
            week_number=1,
            label="Week 1",
            status="scored",
        )
        session.add(week)
        session.flush()

        session.add(WeekEntry(user_id=alice.id, pool_id=pool.id, week_id=week.id, points=10))
        session.add(WeekEntry(user_id=bob.id, pool_id=pool.id, week_id=week.id, points=5))
        session.add(
            PayoutRule(pool_id=pool.id, scope="weekly", place=1, mode="amount", value=Decimal("50"))
        )
        session.commit()
        return pool.id, week.id, alice.id, bob.id
    finally:
        session.close()


def _bare_pool(session_factory, *, join_code: str) -> int:
    session = session_factory()
    try:
        pool = Pool(
            name="Bare Pool",
            join_code=join_code,
            season_year=2026,
            sports=["nfl"],
            timezone="America/New_York",
            current_week=1,
        )
        session.add(pool)
        session.commit()
        return pool.id
    finally:
        session.close()


def test_payouts_preset_seeds_the_known_ladder_and_is_idempotent(isolated_db):
    from app.cli import payouts_preset_cmd

    pool_id = _bare_pool(isolated_db, join_code="PRESET1")

    payouts_preset_cmd(pool_id=pool_id)

    session = isolated_db()
    try:
        rules = session.scalars(select(PayoutRule).where(PayoutRule.pool_id == pool_id)).all()
        assert len(rules) == 12
        pool = session.get(Pool, pool_id)
        assert pool.weekly_payout_weeks == 15
    finally:
        session.close()

    # Re-running must reseed the identical 12 rows, not duplicate or error.
    payouts_preset_cmd(pool_id=pool_id)

    session = isolated_db()
    try:
        rules_again = session.scalars(select(PayoutRule).where(PayoutRule.pool_id == pool_id)).all()
        assert len(rules_again) == 12
    finally:
        session.close()


def test_payouts_show_rejects_an_unknown_scope(isolated_db):
    from app.cli import payouts_show_cmd

    pool_id = _bare_pool(isolated_db, join_code="SHOW1")

    with pytest.raises(typer.Exit) as exc_info:
        payouts_show_cmd(scope="not-a-scope", pool_id=pool_id, year=None)
    assert exc_info.value.exit_code == 1


def test_payouts_show_prints_the_ladder_for_all_four_scopes(isolated_db, capsys):
    from app.cli import payouts_preset_cmd, payouts_show_cmd

    pool_id = _bare_pool(isolated_db, join_code="SHOW2")
    payouts_preset_cmd(pool_id=pool_id)
    capsys.readouterr()  # discard payouts-preset's own output

    payouts_show_cmd(scope=None, pool_id=pool_id, year=None)
    out = capsys.readouterr().out

    for scope_name in ("weekly", "bowl", "season_points", "season_wins"):
        assert scope_name in out
    assert "Grand total" in out
    assert "$4,950.00" in out  # the known preset ladder's grand total, independent of the pot


def test_payouts_show_scope_filter_only_prints_that_scope(isolated_db, capsys):
    from app.cli import payouts_preset_cmd, payouts_show_cmd

    pool_id = _bare_pool(isolated_db, join_code="SHOW3")
    payouts_preset_cmd(pool_id=pool_id)
    capsys.readouterr()

    payouts_show_cmd(scope="bowl", pool_id=pool_id, year=None)
    out = capsys.readouterr().out

    assert "bowl" in out
    assert "weekly" not in out
    assert "season_points" not in out
    assert "season_wins" not in out


def test_payouts_snapshot_requires_week_for_weekly_scope(isolated_db):
    from app.cli import payouts_snapshot_cmd

    pool_id, _week_id, _alice_id, _bob_id = _seed_payout_pool(isolated_db)

    with pytest.raises(typer.Exit) as exc_info:
        payouts_snapshot_cmd(scope="weekly", week=None, year=None, pool_id=pool_id)
    assert exc_info.value.exit_code == 1


def test_payouts_snapshot_rejects_a_week_for_a_season_scope(isolated_db):
    from app.cli import payouts_snapshot_cmd

    pool_id, _week_id, _alice_id, _bob_id = _seed_payout_pool(isolated_db)

    with pytest.raises(typer.Exit) as exc_info:
        payouts_snapshot_cmd(scope="season_points", week=1, year=None, pool_id=pool_id)
    assert exc_info.value.exit_code == 1


def test_payouts_snapshot_creates_awards_and_is_idempotent(isolated_db):
    from app.cli import payouts_snapshot_cmd

    pool_id, _week_id, alice_id, _bob_id = _seed_payout_pool(isolated_db)

    payouts_snapshot_cmd(scope="weekly", week=1, year=None, pool_id=pool_id)

    session = isolated_db()
    try:
        awards = session.scalars(
            select(PayoutAward).where(PayoutAward.pool_id == pool_id, PayoutAward.scope == "weekly")
        ).all()
        assert len(awards) == 1
        assert awards[0].user_id == alice_id
        assert awards[0].amount == Decimal("50.00")
        award_id = awards[0].id
    finally:
        session.close()

    # Re-running with nothing changed underneath must leave the same row alone.
    payouts_snapshot_cmd(scope="weekly", week=1, year=None, pool_id=pool_id)

    session = isolated_db()
    try:
        awards_again = session.scalars(
            select(PayoutAward).where(PayoutAward.pool_id == pool_id, PayoutAward.scope == "weekly")
        ).all()
        assert len(awards_again) == 1
        assert awards_again[0].id == award_id
        assert awards_again[0].amount == Decimal("50.00")
    finally:
        session.close()


def test_payouts_summary_prints_totals_that_reconcile_paid_and_unpaid(isolated_db, capsys):
    from app.cli import payouts_snapshot_cmd, payouts_summary_cmd

    pool_id, _week_id, _alice_id, _bob_id = _seed_payout_pool(isolated_db)
    payouts_snapshot_cmd(scope="weekly", week=1, year=None, pool_id=pool_id)

    session = isolated_db()
    try:
        award = session.scalar(
            select(PayoutAward).where(PayoutAward.pool_id == pool_id, PayoutAward.scope == "weekly")
        )
        award.paid_at = utcnow()
        session.commit()
    finally:
        session.close()

    payouts_summary_cmd(pool_id=pool_id)
    out = capsys.readouterr().out

    assert "Alice" in out
    assert "Bob" in out
    assert "$50.00" in out  # Alice's weekly award, now paid
    assert "Total" in out
