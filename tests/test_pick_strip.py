"""The expandable pick strip on Results and Season (standings and ties, September, Phase 7):
GET /results/pick-strip, the chevron toggle it is loaded from, and the privacy rule it must
never violate (a week's picks are visible to everyone only after that week locks).
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
from app.models import Base, Game, Pick, Pool, PoolMember, User, Week, WeekEntry

UTC = dt.UTC


@pytest.fixture
def engine():
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


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Strip Test Pool",
        "join_code": f"STRIP{id(overrides) % 100000}",
        "season_year": 2026,
        "num_games_per_week": 4,
        "target_nfl": 4,
        "target_ncaaf": 0,
        "picks_required": 4,
        "sports": ["nfl"],
        "auto_publish": True,
        "open_registration": False,
        "timezone": "America/New_York",
        "current_week": 1,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _user(db, email, name) -> User:
    user = User(email=email, password_hash=hash_password("hunter2hunter2"), display_name=name)
    db.add(user)
    db.flush()
    return user


def _week(db, pool, *, week_number, status="open", lock_in_hours) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=week_number,
        label=f"Week {week_number}",
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
        espn_event_id=f"strip-{abbr}-{week.id}",
        start_time=dt.datetime.now(UTC) + dt.timedelta(hours=1),
        home_team=f"Home {abbr}",
        away_team=f"Away {abbr}",
        home_abbr=f"H{abbr}",
        away_abbr=f"A{abbr}",
        canonical_home_key=f"nfl:h{abbr.lower()}",
        canonical_away_key=f"nfl:a{abbr.lower()}",
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


def _pick(db, pool, week, user, game, *, confidence, picked_team="home"):
    db.add(
        Pick(
            user_id=user.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=game.id,
            picked_team=picked_team,
            confidence=confidence,
        )
    )


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/results"}
    )
    assert response.status_code == 303, response.text


def _build_week_with_picks(db, pool, week, boss, player, games):
    for i, game in enumerate(games):
        _pick(db, pool, week, player, game, confidence=i + 1)
    db.add(
        WeekEntry(
            pool_id=pool.id, week_id=week.id, user_id=boss.id, points=0, correct=0, possible=0
        )
    )
    db.add(
        WeekEntry(
            pool_id=pool.id, week_id=week.id, user_id=player.id, points=3, correct=3, possible=4
        )
    )


def test_own_picks_are_revealed_before_lock_but_another_players_are_hidden(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, week_number=1, status="open", lock_in_hours=48)
    games = [_game(db, week, f"G{i}", rank=i + 1) for i in range(4)]
    _build_week_with_picks(db, pool, week, boss, player, games)
    db.commit()
    db.close()

    # Boss requesting player's own panel, before lock: hidden.
    _login(client, "boss@example.com")
    response = client.get(f"/results/pick-strip?week=1&user_id={player.id}")
    assert response.status_code == 200
    assert "Picks are hidden until lock" in response.text
    assert "pick-strip-card" not in response.text

    # The player requesting their OWN panel, still before lock: revealed.
    _login(client, "player@example.com")
    response = client.get(f"/results/pick-strip?week=1&user_id={player.id}")
    assert response.status_code == 200
    assert "Picks are hidden until lock" not in response.text
    assert "pick-strip-card" in response.text


def test_picks_reveal_to_everyone_once_the_week_locks(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, week_number=1, status="locked", lock_in_hours=-1)
    games = [_game(db, week, f"G{i}", rank=i + 1) for i in range(4)]
    _build_week_with_picks(db, pool, week, boss, player, games)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get(f"/results/pick-strip?week=1&user_id={player.id}")
    assert response.status_code == 200
    assert "Picks are hidden until lock" not in response.text
    assert "pick-strip-card" in response.text


def test_cards_are_ordered_by_confidence_descending(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, week_number=1, status="locked", lock_in_hours=-1)
    games = [_game(db, week, f"G{i}", rank=i + 1) for i in range(4)]
    _build_week_with_picks(db, pool, week, boss, player, games)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get(f"/results/pick-strip?week=1&user_id={player.id}")
    text = response.text
    # Picks were built with confidence i+1 against games G0..G3 (game G3 got confidence 4,
    # the highest): the highest confidence card must appear first in the rendered strip, each
    # team abbreviation appearing exactly once, in descending confidence order.
    positions = [text.index(f"HG{i}") for i in (3, 2, 1, 0)]
    assert positions == sorted(positions)


def test_pick_strip_403s_for_a_player_in_another_pool(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    other_pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    stranger = _user(db, "stranger@example.com", "Stranger")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=other_pool.id, user_id=stranger.id, role_in_pool="member"))
    _week(db, pool, week_number=1, status="locked", lock_in_hours=-1)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get(f"/results/pick-strip?week=1&user_id={stranger.id}")
    assert response.status_code == 403


def test_season_week_selector_loads_the_chosen_week(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))

    week1 = _week(db, pool, week_number=1, status="locked", lock_in_hours=-1)
    games1 = [_game(db, week1, f"W1G{i}", rank=i + 1) for i in range(4)]
    _build_week_with_picks(db, pool, week1, boss, player, games1)

    week2 = _week(db, pool, week_number=2, status="locked", lock_in_hours=-1)
    games2 = [_game(db, week2, f"W2G{i}", rank=i + 1) for i in range(4)]
    _build_week_with_picks(db, pool, week2, boss, player, games2)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get(f"/results/pick-strip?week=1&user_id={player.id}&selector=1")
    assert response.status_code == 200
    assert "Week 1" in response.text
    assert "HW1G3" in response.text
    assert "HW2G3" not in response.text

    response2 = client.get(f"/results/pick-strip?week=2&user_id={player.id}&selector=1")
    assert response2.status_code == 200
    assert "Week 2" in response2.text


def test_results_page_wires_the_chevron_to_the_matching_panel(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "Boss")
    player = _user(db, "player@example.com", "Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, week_number=1, status="locked", lock_in_hours=-1)
    games = [_game(db, week, f"G{i}", rank=i + 1) for i in range(4)]
    _build_week_with_picks(db, pool, week, boss, player, games)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/results?week=1")
    assert response.status_code == 200
    text = response.text
    assert f'aria-controls="pick-strip-week-{player.id}"' in text
    assert f'id="pick-strip-week-{player.id}"' in text
    assert 'aria-expanded="false"' in text
    assert f"user_id={player.id}" in text
