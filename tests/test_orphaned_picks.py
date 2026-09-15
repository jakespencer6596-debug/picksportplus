"""The orphaned picks incident: a save must leave the database in exactly the state the
player submitted, never the union of every state they have ever submitted. See
app/routers/picks.py._upsert_picks, app/services/pick_repair.py, and DECISIONS.md,
"Orphaned picks", for the full story.

Local engine/session_factory/client/world fixtures, matching every other integration test
file's own convention (see tests/test_app.py, tests/test_payout_routes.py) rather than
importing fixtures across test modules. world here has a 5 game slate but only 4 picks
required, so a player can genuinely swap which game carries a pick, the shape of the real
reported incident (16 games picked out of a slate bigger than the required 15); test_app.py's
own world fixture sets picks_required equal to its slate size, which cannot represent that.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, Game, Pick, Pool, PoolMember, User, Week, WeekEntry
from app.routers.picks import PickIntegrityError, _upsert_picks
from app.scoring import PickInput

UTC = dt.UTC


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
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


def _make_pool(db: Session, *, num_games: int = 5, picks_required: int = 4) -> Pool:
    pool = Pool(
        name="Orphan Test Pool",
        join_code="ORPHANTEST",
        season_year=2025,
        num_games_per_week=num_games,
        target_nfl=2,
        target_ncaaf=2,
        picks_required=picks_required,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=5,
        payment_required_to_pick=False,
    )
    db.add(pool)
    db.flush()
    return pool


def _make_user(db: Session, email: str, name: str, role: str = "player") -> User:
    user = User(
        email=email,
        password_hash=hash_password("hunter2hunter2"),
        display_name=name,
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def _make_week(db: Session, pool: Pool, *, lock_in_hours: float = 48.0) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=5,
        label="Week 5",
        status="open",
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=lock_in_hours),
    )
    db.add(week)
    db.flush()
    return week


def _make_games(db: Session, week: Week, count: int = 5) -> list[Game]:
    games = []
    base = dt.datetime.now(UTC) + dt.timedelta(hours=48)
    for i in range(count):
        game = Game(
            week_id=week.id,
            league="nfl" if i % 2 == 0 else "ncaaf",
            espn_event_id=f"orphan-evt{i}",
            start_time=base + dt.timedelta(hours=i),
            home_team=f"Home Team {i}",
            away_team=f"Away Team {i}",
            home_abbr=f"H{i}",
            away_abbr=f"A{i}",
            canonical_home_key=f"nfl:orphan-home-{i}",
            canonical_away_key=f"nfl:orphan-away-{i}",
            spread_home=-1.5 - i,
            closeness=1.5 + i,
            spread_source="espn",
            in_slate=True,
            slate_rank=i + 1,
            status="scheduled",
        )
        db.add(game)
        games.append(game)
    db.flush()
    return games


@pytest.fixture
def world(session_factory):
    """A pool with a commissioner, a player, an open week, 5 slate games, and
    picks_required=4, so a player can swap which game they hold a pick on."""
    db = session_factory()
    pool = _make_pool(db)
    boss = _make_user(db, "boss@example.com", "The Commissioner", role="admin")
    player = _make_user(db, "player@example.com", "Regular Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _make_week(db, pool)
    games = _make_games(db, week)
    db.commit()
    data = {
        "pool_id": pool.id,
        "boss_id": boss.id,
        "player_id": player.id,
        "week_id": week.id,
        "game_ids": [g.id for g in games],
    }
    db.close()
    return data


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/picks"}
    )
    assert response.status_code == 303, response.text


def _submission(game_ids: list[int]) -> dict[str, str]:
    """4 picks out of however many game_ids are passed, confidence 1..4."""
    data = {}
    for index, gid in enumerate(game_ids[:4]):
        data[f"winner-{gid}"] = "home" if index % 2 == 0 else "away"
        data[f"confidence-{gid}"] = str(4 - index)
    return data


def test_saving_a_different_set_deletes_the_orphaned_pick(client, world, session_factory):
    """The regression test for the orphaned picks incident: save 4 of 5 games, then save a
    different 4 (one game swapped out for another), and the database must hold exactly 4
    rows, with the dropped game's pick gone. Before the fix, _upsert_picks left the old row
    behind, growing the count to 5 and letting two picks share a confidence value.
    """
    game_ids = world["game_ids"]
    _login(client, "player@example.com")

    response = client.post("/picks", data=_submission(game_ids[0:4]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == 4
    db.close()

    # Swap: drop game_ids[0], pick game_ids[4] instead.
    second_ids = game_ids[1:5]
    response = client.post("/picks", data=_submission(second_ids))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == 4, f"expected exactly 4 picks, found {len(picks)}"
    picked_game_ids = {p.game_id for p in picks}
    assert picked_game_ids == set(second_ids)
    assert game_ids[0] not in picked_game_ids
    assert sorted(p.confidence for p in picks) == [1, 2, 3, 4]
    db.close()


def test_saving_a_completely_different_set_still_leaves_exactly_n_rows(
    client, world, session_factory
):
    game_ids = world["game_ids"]
    _login(client, "player@example.com")

    client.post("/picks", data=_submission(game_ids[0:4]))
    response = client.post("/picks", data=_submission(game_ids[1:5]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == 4
    assert {p.game_id for p in picks} == set(game_ids[1:5])
    db.close()


def test_reversing_confidence_order_on_the_same_games_never_trips_the_unique_constraint(
    client, world, session_factory
):
    """No game added or removed, just every ranking reversed: the intermediate states this
    write passes through (one row at a time) must never collide with the new
    uq_pick_user_week_confidence constraint, even though neither the old nor the new state
    ever violates it."""
    game_ids = world["game_ids"][0:4]
    _login(client, "player@example.com")

    first = {}
    for index, gid in enumerate(game_ids):
        first[f"winner-{gid}"] = "home"
        first[f"confidence-{gid}"] = str(index + 1)
    assert client.post("/picks", data=first).status_code == 303

    reversed_data = {}
    for index, gid in enumerate(game_ids):
        reversed_data[f"winner-{gid}"] = "home"
        reversed_data[f"confidence-{gid}"] = str(4 - index)
    response = client.post("/picks", data=reversed_data)
    assert response.status_code == 303

    db = session_factory()
    picks = {
        p.game_id: p.confidence
        for p in db.scalars(select(Pick).where(Pick.user_id == world["player_id"]))
    }
    assert len(picks) == 4
    for index, gid in enumerate(game_ids):
        assert picks[gid] == 4 - index
    db.close()


def test_lock_path_also_deletes_orphans(client, world, session_factory):
    """_upsert_picks is shared by /picks and /picks/lock; confirm the lock route gets the
    same fix, not just save."""
    game_ids = world["game_ids"]
    _login(client, "player@example.com")

    client.post("/picks", data=_submission(game_ids[0:4]))
    response = client.post("/picks/lock", data=_submission(game_ids[1:5]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == 4
    assert {p.game_id for p in picks} == set(game_ids[1:5])
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


def test_a_player_with_no_prior_picks_is_unaffected(client, world, session_factory):
    """A first ever save (no existing rows for this user/week) is not touched by the
    delete-the-orphans logic: there is nothing to delete, and the save still writes exactly
    the submitted picks."""
    game_ids = world["game_ids"]
    _login(client, "player@example.com")
    response = client.post("/picks", data=_submission(game_ids[0:4]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == 4
    assert {p.game_id for p in picks} == set(game_ids[0:4])
    db.close()


def test_post_write_assertion_rolls_back_a_corrupt_state(world, session_factory):
    """_upsert_picks re-reads the row set it just wrote and raises PickIntegrityError if it is
    not exactly picks_required picks forming a clean permutation, rather than committing.
    validate_picks already guarantees a real submission can never reach this in practice; this
    calls _upsert_picks directly with a submission that does not match pool.picks_required,
    the only way to actually exercise that this backstop works.
    """
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    user = db.get(User, world["player_id"])
    game_ids = world["game_ids"]

    # Only 2 picks submitted while picks_required is 4: never a state validate_picks would
    # let through the real routes, exactly what the post write assertion exists to catch.
    bad_submission = [
        PickInput(game_id=game_ids[0], picked_team="home", confidence=1),
        PickInput(game_id=game_ids[1], picked_team="away", confidence=2),
    ]
    with pytest.raises(PickIntegrityError):
        _upsert_picks(db, user, pool, week, bad_submission, dt.datetime.now(UTC))
    db.rollback()

    picks = list(db.scalars(select(Pick).where(Pick.user_id == user.id, Pick.week_id == week.id)))
    assert picks == []
    db.close()


def test_unique_constraint_rejects_a_duplicate_confidence_value(world, session_factory):
    """The database level backstop (uq_pick_user_week_confidence): two Pick rows for the same
    user and week can never share a confidence value, even bypassing the ORM's own guards."""
    from sqlalchemy.exc import IntegrityError

    db = session_factory()
    game_ids = world["game_ids"]
    db.add(
        Pick(
            user_id=world["player_id"],
            pool_id=world["pool_id"],
            week_id=world["week_id"],
            game_id=game_ids[0],
            picked_team="home",
            confidence=1,
        )
    )
    db.commit()

    db.add(
        Pick(
            user_id=world["player_id"],
            pool_id=world["pool_id"],
            week_id=world["week_id"],
            game_id=game_ids[1],
            picked_team="away",
            confidence=1,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


# Phase 2: the browser already caps how many games can be picked (pickedRowCount(list) >=
# required in app.js's onClick, from an earlier phase) and already shows a live "X of N"
# count (updateSummary). Neither needed a change for this incident: the bug was never that a
# player could get more than picks_required winners selected in one sitting, it was that the
# server union'd two separate, individually valid sittings together (Phase 1, above). What
# was still missing is a test proving the server rejects an oversized submission in the exact
# shape of the reported incident, a hand crafted POST no real client would ever send, matching
# the commissioner's own reported "16 ranked games when the league requires 15."


@pytest.fixture
def sixteen_game_world(session_factory):
    """16 slate games, picks_required=15, matching the reported incident's own numbers."""
    db = session_factory()
    pool = _make_pool(db, num_games=16, picks_required=15)
    boss = _make_user(db, "boss@example.com", "The Commissioner", role="admin")
    player = _make_user(db, "player@example.com", "Regular Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _make_week(db, pool)
    games = _make_games(db, week, count=16)
    db.commit()
    data = {
        "pool_id": pool.id,
        "week_id": week.id,
        "player_id": player.id,
        "game_ids": [g.id for g in games],
    }
    db.close()
    return data


def test_server_rejects_a_hand_crafted_16_pick_post_when_15_are_required(
    client, sixteen_game_world
):
    """The browser cap and live count are convenience, never authority (Phase 2). A
    hand crafted POST covering all 16 slate games, exactly the shape of the reported
    incident, must still be rejected server side even though no real client, with the
    existing cap in place, would ever produce it."""
    game_ids = sixteen_game_world["game_ids"]
    _login(client, "player@example.com")
    data = {}
    for index, gid in enumerate(game_ids):
        data[f"winner-{gid}"] = "home" if index % 2 == 0 else "away"
        data[f"confidence-{gid}"] = str(index + 1)
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "You have picked 16 games. Pick 15." in response.text

    db_check = client.get("/picks")
    assert "16 of 15" not in db_check.text
