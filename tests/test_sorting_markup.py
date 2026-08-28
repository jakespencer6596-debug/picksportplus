"""Server-rendered markup for Phase 4 (sorting on the slate editor and picks page, see
PERF-REPORT.md and DECISIONS.md). The JS test suite (tests/js/sorting.test.js) covers actual
sort behavior in a real DOM; this covers that the server actually emits the attributes and
controls that behavior depends on.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, Game, Pool, PoolMember, User, Week

UTC = dt.UTC


@pytest.fixture
def slate_client():
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
        name="Sort Test Pool",
        join_code="SORTTEST",
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
    player = User(
        email="player@example.com",
        password_hash=hash_password("hunter2hunter2"),
        display_name="Regular Player",
        role="player",
    )
    db.add(boss)
    db.add(player)
    db.flush()
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))

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
    for i, league in enumerate(("nfl", "ncaaf")):
        db.add(
            Game(
                week_id=week.id,
                league=league,
                espn_event_id=f"evt{i}",
                start_time=base_time + dt.timedelta(hours=i),
                home_team=f"Home {i}",
                away_team=f"Away {i}",
                home_abbr=f"H{i}",
                away_abbr=f"A{i}",
                canonical_home_key=f"{league}:home-{i}",
                canonical_away_key=f"{league}:away-{i}",
                spread_home=-1.5,
                closeness=1.5,
                spread_source="espn",
                in_slate=True,
                slate_rank=i + 1,
                status="scheduled",
            )
        )
    db.add(
        Game(
            week_id=week.id,
            league="nfl",
            espn_event_id="evt-candidate",
            start_time=base_time + dt.timedelta(hours=5),
            home_team="Home C",
            away_team="Away C",
            home_abbr="HC",
            away_abbr="AC",
            canonical_home_key="nfl:home-c",
            canonical_away_key="nfl:away-c",
            spread_home=-7.0,
            closeness=7.0,
            spread_source="espn",
            in_slate=False,
            status="scheduled",
        )
    )
    db.commit()
    db.close()

    with TestClient(app, follow_redirects=False) as client:
        yield client

    app.dependency_overrides.clear()


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/picks"}
    )
    assert response.status_code == 303, response.text


def test_slate_editor_tables_carry_sortable_columns_and_mobile_selects(slate_client):
    _login(slate_client, "boss@example.com")
    response = slate_client.get("/league/slate?week=5")
    assert response.status_code == 200
    body = response.text

    assert 'id="on-slate-table"' in body
    assert 'id="candidates-table"' in body
    # Every dimension the brief names: kickoff date/time, league, closeness, spread source,
    # matchup name.
    for table_id in ("on-slate-table", "candidates-table"):
        assert f'data-sort-select-for="{table_id}"' in body

    # Numeric/datetime columns sort by a raw value, never rendered text.
    assert "data-sort-value=" in body

    # aria-sort starts correct on the default-sorted column for each table.
    assert 'aria-sort="ascending"' in body


def test_picks_page_has_a_sort_control_and_per_dimension_data_attributes(slate_client):
    _login(slate_client, "player@example.com")
    response = slate_client.get("/picks")
    assert response.status_code == 200
    body = response.text

    assert "data-game-sort-select" in body
    assert "data-game-sort-reset" in body
    for key in ("slate", "kickoff", "league", "closeness", "source", "matchup"):
        assert f"data-sort-{key}=" in body

    # Default option is slate order, matching the brief: "Default stays the commissioner's
    # slate rank order."
    assert '<option value="slate:asc">Slate order</option>' in body
