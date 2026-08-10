"""Tests for app/cli.py's seed_admin.

Phase 5 flipped Pool.auto_publish's model default to False and removed the hard coded
auto_publish=True from seed_admin's Pool(...) call (app/cli.py). Without that second fix, a
freshly seeded pool would have kept silently auto publishing forever, model default or not.
This is the one CLI command tested in this repo directly (through app.db.session_scope,
pointed at a throwaway database), specifically to prove that end to end rather than only at
the model layer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base, Pool, PoolMember, User


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
