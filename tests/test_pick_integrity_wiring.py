"""Phase 4 of the orphaned picks incident: surfacing corrupt pick data before it can do any
more damage, rather than relying on a commissioner spotting it by eye in a results grid (how
this incident was actually found). Covers the commissioner dashboard warning
(app/routers/admin.py) and the scored-week guard (app/services/results.py). The cron exit
code check lives in tests/test_cli.py, next to every other run-cron test.
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
from app.models import Base, Game, PayoutAward, Pick, Pool, PoolMember, User, Week
from app.services.results import score_week_for_pool

UTC = dt.UTC


@pytest.fixture
def engine():
    """The corrupt rows these tests insert are 5 picks with 5 distinct confidence values
    (1..5) against a 4-pick requirement: too many rows, never a duplicate value, so
    uq_pick_user_week_confidence is not in play here and the ordinary schema is fine."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def client(session_factory):
    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


def _pool(db) -> Pool:
    pool = Pool(
        name="Wiring Test Pool",
        join_code="WIRINGTEST",
        season_year=2025,
        num_games_per_week=5,
        target_nfl=3,
        target_ncaaf=2,
        picks_required=4,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=1,
    )
    db.add(pool)
    db.flush()
    return pool


def _user(db, email, name) -> User:
    user = User(email=email, password_hash=hash_password("hunter2hunter2"), display_name=name)
    db.add(user)
    db.flush()
    return user


def _week(db, pool, *, status="open", lock_in_hours=48.0) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=1,
        label="Week 1",
        status=status,
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=lock_in_hours),
    )
    db.add(week)
    db.flush()
    return week


def _game(db, week, abbr, *, rank, status="scheduled", winner=None) -> Game:
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id=f"wiring-{abbr}-{week.id}",
        start_time=dt.datetime.now(UTC) + dt.timedelta(hours=48),
        home_team=abbr,
        away_team=f"Opp{abbr}",
        home_abbr=abbr,
        away_abbr=f"O{abbr}",
        canonical_home_key=f"nfl:{abbr.lower()}",
        canonical_away_key=f"nfl:o{abbr.lower()}",
        spread_home=-3.0,
        closeness=3.0,
        spread_source="espn",
        in_slate=True,
        slate_rank=rank,
        status=status,
        winner=winner,
    )
    db.add(game)
    db.flush()
    return game


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/league"}
    )
    assert response.status_code == 303, response.text


def _add_corrupt_picks(db, pool, week, user, games) -> None:
    """5 picks for a pool that requires 4, confidence 1..5 (an extra row, the orphaned picks
    shape), all sharing one updated_at."""
    now = dt.datetime.now(UTC)
    for i, game in enumerate(games):
        db.add(
            Pick(
                user_id=user.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=i + 1,
                created_at=now,
                updated_at=now,
            )
        )


def test_dashboard_warns_on_a_corrupt_entry(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner")
    player = _user(db, "player@example.com", "Corrupt Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")
    games = [_game(db, week, f"G{i}", rank=i + 1) for i in range(5)]
    _add_corrupt_picks(db, pool, week, player, games)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "Pick data needs attention" in response.text
    assert "Corrupt Player" in response.text


def test_dashboard_shows_no_warning_for_a_clean_pool(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner")
    player = _user(db, "player@example.com", "Clean Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")
    games = [_game(db, week, f"G{i}", rank=i + 1) for i in range(4)]
    now = dt.datetime.now(UTC)
    for i, game in enumerate(games):
        db.add(
            Pick(
                user_id=player.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=i + 1,
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "Pick data needs attention" not in response.text


def test_scoring_guard_flags_rather_than_silently_scoring(session_factory):
    """Every slate game goes final while a player's picks are still corrupt: the week must
    not flip to "scored" and no PayoutAward may be written, even though every game is done."""
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner")
    player = _user(db, "player@example.com", "Corrupt Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")
    games = [_game(db, week, f"G{i}", rank=i + 1, status="final", winner="home") for i in range(5)]
    _add_corrupt_picks(db, pool, week, player, games)
    db.commit()

    report = score_week_for_pool(db, pool, week)
    db.commit()

    assert report.integrity_warnings != []
    assert week.status != "scored", "a corrupt entry must hold the week back from scoring"
    assert week.scored_at is None
    awards = list(db.scalars(select(PayoutAward).where(PayoutAward.pool_id == pool.id)))
    assert awards == [], "no payout may freeze while a corrupt entry exists"
    db.close()


def test_scoring_guard_does_not_block_a_clean_week(session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner")
    player = _user(db, "player@example.com", "Clean Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")
    games = [_game(db, week, f"G{i}", rank=i + 1, status="final", winner="home") for i in range(4)]
    now = dt.datetime.now(UTC)
    for i, game in enumerate(games):
        db.add(
            Pick(
                user_id=player.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=i + 1,
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()

    report = score_week_for_pool(db, pool, week)
    db.commit()

    assert report.integrity_warnings == []
    assert week.status == "scored"
    db.close()
