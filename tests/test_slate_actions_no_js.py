"""Phase 5 regression sweep, item 14: every slate action still works with JavaScript
disabled, via the full POST fallback (Phase 1 rewrote these routes to also support an HTMX
partial response; this proves the plain, no-JS path was not broken by that rewrite).

A plain POST (no HX-Request header) must still flash-and-redirect (303) exactly as it always
has, and the underlying mutation must actually happen, for every one of pin, unpin, add,
remove, swap, void, unvoid and spread.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, Game, Pool, PoolMember, User, Week

UTC = dt.UTC


@pytest.fixture
def actions_world():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db

    db = session_factory()
    pool = Pool(
        name="No JS Pool",
        join_code="NOJSPOOL",
        season_year=2025,
        num_games_per_week=2,
        target_nfl=1,
        target_ncaaf=1,
        picks_required=2,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        timezone="America/New_York",
        current_week=5,
        payment_required_to_pick=False,
    )
    db.add(pool)
    db.flush()
    boss = User(
        email="boss@example.com",
        password_hash=hash_password("hunter2hunter2"),
        display_name="The Commissioner",
        role="admin",
    )
    db.add(boss)
    db.flush()
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))

    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=5,
        label="Week 5",
        status="open",
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=48),
    )
    db.add(week)
    db.flush()

    base_time = dt.datetime.now(UTC) + dt.timedelta(hours=48)

    def make_game(espn_id, *, in_slate, slate_rank=None):
        return Game(
            week_id=week.id,
            league="nfl",
            espn_event_id=espn_id,
            start_time=base_time,
            home_team="Home",
            away_team="Away",
            home_abbr="H",
            away_abbr="A",
            canonical_home_key=f"nfl:home-{espn_id}",
            canonical_away_key=f"nfl:away-{espn_id}",
            spread_home=-1.5,
            closeness=1.5,
            spread_source="espn",
            in_slate=in_slate,
            slate_rank=slate_rank,
            status="scheduled",
        )

    on_slate_game = make_game("on-slate", in_slate=True, slate_rank=1)
    candidate_game = make_game("candidate", in_slate=False)
    db.add(on_slate_game)
    db.add(candidate_game)
    db.commit()

    ids = {"week_id": week.id, "on_slate_id": on_slate_game.id, "candidate_id": candidate_game.id}
    db.close()

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"email": "boss@example.com", "password": "hunter2hunter2", "next": "/picks"},
        )
        assert response.status_code == 303, response.text
        yield client, session_factory, ids

    app.dependency_overrides.clear()


def _game(session_factory, game_id):
    db = session_factory()
    try:
        return db.scalar(select(Game).where(Game.id == game_id))
    finally:
        db.close()


def test_pin_and_unpin_work_without_javascript(actions_world):
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["on_slate_id"], "action": "pin"},
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["on_slate_id"]).pinned is True

    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["on_slate_id"], "action": "unpin"},
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["on_slate_id"]).pinned is False


def test_void_and_unvoid_work_without_javascript(actions_world):
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["on_slate_id"], "action": "void"},
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["on_slate_id"]).status == "void"

    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["on_slate_id"], "action": "unvoid"},
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["on_slate_id"]).status != "void"


def test_spread_works_without_javascript(actions_world):
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={
            "week_id": ids["week_id"],
            "game_id": ids["on_slate_id"],
            "action": "spread",
            "spread": "-4.5",
        },
    )
    assert response.status_code == 303
    game = _game(session_factory, ids["on_slate_id"])
    assert game.spread_home == -4.5
    assert game.spread_source == "manual"


def test_add_and_remove_work_without_javascript(actions_world):
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["candidate_id"], "action": "add"},
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["candidate_id"]).in_slate is True

    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["candidate_id"], "action": "remove"},
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["candidate_id"]).in_slate is False


def test_swap_works_without_javascript(actions_world):
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={
            "week_id": ids["week_id"],
            "game_id": ids["on_slate_id"],
            "action": "swap",
            "swap_with": ids["candidate_id"],
        },
    )
    assert response.status_code == 303
    assert _game(session_factory, ids["on_slate_id"]).in_slate is False
    assert _game(session_factory, ids["candidate_id"]).in_slate is True


def test_add_via_htmx_returns_oob_table_refreshes_not_the_whole_page(actions_world):
    """The membership-changing actions (add/remove/swap) cannot express "this row moved to a
    different table" as a single-element swap, so they OOB-refresh both tables in full
    instead (Phase 1). Confirms that fragment is what actually comes back, distinct from the
    single-row swap pin/void/spread use (see test_pin_action_is_a_small_htmx_partial_not_the_
    whole_page in tests/test_slate_performance.py)."""
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["candidate_id"], "action": "add"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert 'id="on-slate-tbody"' in response.text
    assert 'id="candidates-tbody"' in response.text
    assert 'id="week-summary-body"' in response.text
    assert _game(session_factory, ids["candidate_id"]).in_slate is True


def test_htmx_error_returns_an_inline_banner_not_a_redirect(actions_world):
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={"week_id": ids["week_id"], "game_id": ids["on_slate_id"], "action": "swap"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "Choose a game to swap in." in response.text
    assert 'id="slate-action-error"' in response.text
    assert _game(session_factory, ids["on_slate_id"]).in_slate is True


def test_an_error_without_javascript_still_flashes_and_redirects(actions_world):
    """The no-JS fallback path must still surface a validation error as a flash, not a crash,
    even though the HTMX path now handles this same error differently (an inline banner)."""
    client, session_factory, ids = actions_world
    response = client.post(
        "/league/slate/game",
        data={
            "week_id": ids["week_id"],
            "game_id": ids["on_slate_id"],
            "action": "swap",
            # No swap_with: ingest.SlateLocked/ValueError path.
        },
    )
    assert response.status_code == 303
    # Nothing changed: the swap never happened.
    assert _game(session_factory, ids["on_slate_id"]).in_slate is True
