"""Integration tests: the app boots, the pages render, and the rules actually hold.

These exercise the real routers, the real templates and the real scoring path against an
in memory database. Nothing here touches the network, the session wide offline fixture in
conftest guarantees it.
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
    """A TestClient wired to the throwaway database."""

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


# Fixtures that build a small but complete pool ------------------------------


def _make_pool(db: Session, *, num_games: int = 4) -> Pool:
    pool = Pool(
        name="Test Pool",
        join_code="TESTCODE",
        season_year=2025,
        num_games_per_week=num_games,
        target_nfl=2,
        target_ncaaf=2,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=5,
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


def _make_week(
    db: Session, pool: Pool, *, lock_in_hours: float = 48.0, status: str = "open"
) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=5,
        label="Week 5",
        status=status,
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=lock_in_hours),
    )
    db.add(week)
    db.flush()
    return week


def _make_games(db: Session, week: Week, count: int = 4) -> list[Game]:
    games = []
    base = dt.datetime.now(UTC) + dt.timedelta(hours=48)
    for i in range(count):
        game = Game(
            week_id=week.id,
            league="nfl" if i % 2 == 0 else "ncaaf",
            espn_event_id=f"evt{i}",
            start_time=base + dt.timedelta(hours=i),
            home_team=f"Home Team {i}",
            away_team=f"Away Team {i}",
            home_abbr=f"H{i}",
            away_abbr=f"A{i}",
            canonical_home_key=f"nfl:home-{i}",
            canonical_away_key=f"nfl:away-{i}",
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
    """A pool with a commissioner, a player, an open week and four slate games."""
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


# Boot and public pages ------------------------------------------------------


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_root_redirects_to_login_when_signed_out(client):
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    # The design system must actually be wired up.
    assert "app.css" in response.text


def test_register_page_renders(client):
    response = client.get("/register")
    assert response.status_code == 200
    assert "join code" in response.text.lower()


def test_protected_page_redirects_signed_out_user(client):
    response = client.get("/picks")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# Auth -----------------------------------------------------------------------


def test_login_rejects_bad_password(client, world):
    response = client.post(
        "/login", data={"email": "player@example.com", "password": "wrong", "next": "/picks"}
    )
    assert response.status_code == 400
    assert "do not match" in response.text


def test_login_does_not_reveal_whether_an_account_exists(client, world):
    missing = client.post(
        "/login", data={"email": "nobody@example.com", "password": "wrong", "next": "/"}
    )
    wrong = client.post(
        "/login", data={"email": "player@example.com", "password": "wrong", "next": "/"}
    )
    assert missing.status_code == wrong.status_code == 400
    assert "do not match an account" in missing.text
    assert "do not match an account" in wrong.text


def test_register_requires_a_valid_join_code_in_private_mode(client, world):
    response = client.post(
        "/register",
        data={
            "display_name": "New Person",
            "email": "new@example.com",
            "password": "hunter2hunter2",
            "join_code": "NOPE",
        },
    )
    assert response.status_code == 400
    assert "join code" in response.text.lower()


def test_register_with_the_right_code_joins_the_pool(client, world, session_factory):
    response = client.post(
        "/register",
        data={
            "display_name": "New Person",
            "email": "new@example.com",
            "password": "hunter2hunter2",
            "join_code": "testcode",  # case insensitive on purpose
        },
    )
    assert response.status_code == 303
    db = session_factory()
    user = db.scalar(select(User).where(User.email == "new@example.com"))
    assert user is not None
    member = db.scalar(select(PoolMember).where(PoolMember.user_id == user.id))
    assert member is not None and member.role_in_pool == "member"
    db.close()


# The signed-in pages render -------------------------------------------------


@pytest.mark.parametrize("path", ["/picks", "/standings", "/results"])
def test_member_pages_render(client, world, path):
    _login(client, "player@example.com")
    response = client.get(path)
    assert response.status_code == 200, response.text[:800]


@pytest.mark.parametrize("path", ["/admin", "/admin/slate", "/admin/members", "/admin/settings"])
def test_admin_pages_render_for_commissioner(client, world, path):
    _login(client, "boss@example.com")
    response = client.get(path)
    assert response.status_code == 200, response.text[:800]


@pytest.mark.parametrize("path", ["/admin", "/admin/slate", "/admin/members", "/admin/settings"])
def test_admin_pages_refused_for_a_regular_player(client, world, path):
    _login(client, "player@example.com")
    response = client.get(path)
    assert response.status_code == 403


# Picks ----------------------------------------------------------------------


def _valid_submission(game_ids: list[int]) -> dict[str, str]:
    n = len(game_ids)
    data = {}
    for index, gid in enumerate(game_ids):
        data[f"winner-{gid}"] = "home" if index % 2 == 0 else "away"
        data[f"confidence-{gid}"] = str(n - index)
    return data


def test_saving_a_valid_entry_stores_every_pick(client, world, session_factory):
    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == len(world["game_ids"])
    assert sorted(p.confidence for p in picks) == [1, 2, 3, 4]
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is not None and entry.submitted_at is not None
    db.close()


def test_duplicate_confidence_is_rejected(client, world):
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    first, second = world["game_ids"][0], world["game_ids"][1]
    data[f"confidence-{second}"] = data[f"confidence-{first}"]
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "used twice" in response.text


def test_a_missing_winner_is_rejected(client, world):
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    dropped = world["game_ids"][0]
    del data[f"winner-{dropped}"]
    del data[f"confidence-{dropped}"]
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "missing a winner" in response.text


def test_partial_entry_saves_nothing(client, world, session_factory):
    """An invalid submission must not leave half an entry behind."""
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    del data[f"winner-{world['game_ids'][0]}"]
    del data[f"confidence-{world['game_ids'][0]}"]
    client.post("/picks", data=data)

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_picks_are_editable_before_lock(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    changed = _valid_submission(world["game_ids"])
    first = world["game_ids"][0]
    changed[f"winner-{first}"] = "away"
    response = client.post("/picks", data=changed)
    assert response.status_code == 303

    db = session_factory()
    pick = db.scalar(select(Pick).where(Pick.user_id == world["player_id"], Pick.game_id == first))
    assert pick.picked_team == "away"
    # Still exactly one row per game, not a duplicate.
    assert len(list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))) == 4
    db.close()


def test_lock_is_enforced_by_the_clock_not_the_status(client, world, session_factory):
    """A late cron must never hand anyone extra time."""
    db = session_factory()
    week = db.get(Week, world["week_id"])
    # Still marked open, but the lock time has passed.
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "open"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 403
    assert "locked" in response.text.lower()


def test_picks_page_is_read_only_after_lock(client, world, session_factory):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "data-sortable" not in response.text


# Results and scoring --------------------------------------------------------


def test_picks_stay_private_until_lock(client, world, session_factory):
    """Another member must not be able to see anyone's picks before the lock."""
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    client.post("/logout")

    # Sign in as someone else. Their own name is in the top bar, so look for the player's.
    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Regular Player" not in response.text


def test_picks_are_revealed_after_lock(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    client.post("/logout")
    _login(client, "boss@example.com")

    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    db.commit()
    db.close()

    response = client.get("/results")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_scoring_end_to_end(client, world, session_factory):
    """Save picks, finalise the games, score, and check the leaderboard math."""
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))

    # The player picked home, away, home, away with confidence 4, 3, 2, 1.
    # Make the first two correct and the last two wrong. Expected points: 4 + 3 = 7.
    outcomes = ["home", "away", "away", "home"]
    for game, winner in zip(games, outcomes, strict=False):
        game.status = "final"
        game.winner = winner
        game.home_score = 21 if winner == "home" else 17
        game.away_score = 17 if winner == "home" else 21
    db.commit()

    report = score_week_for_pool(db, pool, week)
    db.commit()

    entry = db.scalar(
        select(WeekEntry).where(
            WeekEntry.user_id == world["player_id"], WeekEntry.week_id == week.id
        )
    )
    assert entry.points == 7
    assert entry.correct == 2
    assert entry.possible == 4
    assert entry.is_winner is True
    assert report.week_complete is True
    assert db.get(Week, week.id).status == "scored"
    db.close()

    response = client.get("/standings")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_scoring_is_idempotent(client, world, session_factory):
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    for game in db.scalars(select(Game).where(Game.week_id == week.id)):
        game.status = "final"
        game.winner = "home"
        game.home_score, game.away_score = 24, 20
    db.commit()

    first = score_week_for_pool(db, pool, week)
    db.commit()
    entry_a = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"])).points
    second = score_week_for_pool(db, pool, week)
    db.commit()
    entry_b = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"])).points

    assert entry_a == entry_b
    assert first.players == second.players
    # Exactly one entry row, not one per run.
    rows = list(db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id)))
    assert len(rows) == 2  # the commissioner and the player, one row each
    db.close()


def test_a_voided_game_scores_nobody_and_leaves_the_possible_count(client, world, session_factory):
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))
    for game in games:
        game.status = "final"
        game.winner = "home"
        game.home_score, game.away_score = 24, 20
    # The player staked 4 points on game one and picked home, which would have been correct.
    games[0].status = "void"
    games[0].winner = None
    db.commit()

    score_week_for_pool(db, pool, week)
    db.commit()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    # home picks were games 1 and 3 with confidence 4 and 2. Game 1 is void, so only 2 scores.
    assert entry.points == 2
    assert entry.correct == 1
    assert entry.possible == 3
    db.close()


# Commissioner controls ------------------------------------------------------


def test_commissioner_can_change_the_slate_size(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/settings",
        data={
            "name": "Renamed League",
            "season_year": "2025",
            "timezone": "America/Chicago",
            "num_games_per_week": "18",
            "target_nfl": "6",
            "target_ncaaf": "12",
            "auto_publish": "1",
            "open_registration": "",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.name == "Renamed League"
    assert pool.num_games_per_week == 18
    assert pool.target_nfl == 6
    assert pool.target_ncaaf == 12
    assert pool.timezone == "America/Chicago"
    assert pool.auto_publish is True
    assert pool.open_registration is False
    db.close()


def test_slate_cannot_be_resized_once_a_pick_exists(client, world, session_factory):
    from app.services.ingest import SlateLocked, remove_from_slate

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    week = db.get(Week, world["week_id"])
    with pytest.raises(SlateLocked):
        remove_from_slate(db, week, world["game_ids"][0])
    db.close()


def test_voiding_stays_available_after_picks_exist(client, world, session_factory):
    from app.services.ingest import set_void

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    week = db.get(Week, world["week_id"])
    game = set_void(db, week, world["game_ids"][0], True)
    db.commit()
    assert game.status == "void"
    db.close()


def test_validation_errors_can_actually_reach_the_browser():
    """htmx does not swap a 4xx by default, so this wiring is load bearing.

    Without the document level htmx:beforeSwap handler the server's validation messages
    are computed, returned, and then silently dropped: the player sees a "Not saved" chip
    and no reason. It has to be a document listener, because on an error response htmx
    dispatches beforeSwap at the target rather than at the element that triggered the
    request, so the same code as an hx-on attribute on the save button never runs.
    """
    from app.templating import STATIC_DIR, TEMPLATES_DIR

    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("htmx:beforeSwap"' in app_js
    assert "shouldSwap = true" in app_js

    picks = (TEMPLATES_DIR / "picks.html").read_text(encoding="utf-8")
    assert "hx-on::before-swap" not in picks, (
        "An hx-on::before-swap handler on the save button never fires for a 4xx. "
        "Keep this in app.js as a document level listener."
    )


def test_the_error_partial_lists_each_problem_separately(client, world):
    """The partial the browser swaps in must name every problem, not just the first."""
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    first, second = world["game_ids"][0], world["game_ids"][1]
    data[f"confidence-{second}"] = data[f"confidence-{first}"]

    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert response.text.count("<li>") >= 2
    assert 'role="alert"' in response.text
    assert "used twice" in response.text
    assert "is not used" in response.text


def test_join_code_rotation_invalidates_the_old_code(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post("/admin/join-code")
    assert response.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.join_code != "TESTCODE"
    db.close()

    client.post("/logout")
    rejected = client.post(
        "/register",
        data={
            "display_name": "Too Late",
            "email": "late@example.com",
            "password": "hunter2hunter2",
            "join_code": "TESTCODE",
        },
    )
    assert rejected.status_code == 400
