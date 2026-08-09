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
from app.models import Base, Game, PayoutRule, Pick, Pool, PoolMember, User, Week, WeekEntry

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


def _make_pool(
    db: Session,
    *,
    num_games: int = 4,
    picks_required: int | None = None,
    payment_required_to_pick: bool = False,
) -> Pool:
    # picks_required defaults to num_games so _valid_submission (which submits every game
    # in world["game_ids"]) stays a valid, complete entry unless a test deliberately wants
    # picks_required to be smaller than the slate, proving it is a real, honored setting.
    # payment_required_to_pick defaults to False (the model's own real default is True, Phase
    # 7) so every pre-existing test that posts picks through the world fixture keeps working
    # unless a test deliberately wants the Venmo gate in play, the same reasoning
    # picks_required's own default documents just above.
    pool = Pool(
        name="Test Pool",
        join_code="TESTCODE",
        season_year=2025,
        num_games_per_week=num_games,
        target_nfl=2,
        target_ncaaf=2,
        picks_required=picks_required if picks_required is not None else num_games,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=5,
        payment_required_to_pick=payment_required_to_pick,
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


def test_root_renders_landing_page_when_signed_out(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "The closest games, every week" in response.text
    assert "Pricing" in response.text
    # The signed-out header, not the signed-in one.
    assert 'href="/login"' in response.text
    assert "This Week" not in response.text


def test_root_redirects_to_picks_when_signed_in(client, world):
    _login(client, "player@example.com")
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/picks"


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    # The design system must actually be wired up.
    assert "app.css" in response.text


def test_pricing_page_renders_signed_out(client):
    response = client.get("/pricing")
    assert response.status_code == 200
    assert "199" in response.text
    assert "349" in response.text
    assert "398" in response.text
    assert "50" in response.text
    assert 'href="/login"' in response.text


def test_pricing_page_renders_signed_in(client, world):
    _login(client, "player@example.com")
    response = client.get("/pricing")
    assert response.status_code == 200
    assert "199" in response.text
    assert "Regular Player" in response.text  # the signed-in header, not the public one


def test_how_it_works_page_renders(client, world):
    response = client.get("/how-it-works")
    assert response.status_code == 200
    assert "inverse" in response.text.lower()

    _login(client, "player@example.com")
    response = client.get("/how-it-works")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_contact_page_renders(client, world):
    response = client.get("/contact")
    assert response.status_code == 200

    _login(client, "player@example.com")
    response = client.get("/contact")
    assert response.status_code == 200
    assert "Regular Player" in response.text


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


def test_open_picks_page_includes_row_expand_toggle(client, world):
    # Post launch: desktop compact rows (1024px up) move the badge, line, kickoff and
    # each team's record into a collapsed per row panel, revealed by a new toggle button.
    # One toggle and one panel per slate game, each pair wired together by id.
    _login(client, "player@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    game_ids = world["game_ids"]
    assert response.text.count("data-row-toggle") == len(game_ids)
    assert response.text.count("data-row-detail") == len(game_ids)
    for gid in game_ids:
        assert f'aria-controls="game-detail-{gid}"' in response.text
        assert f'id="game-detail-{gid}"' in response.text


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


def test_an_incomplete_submission_is_rejected(client, world):
    """Phase 3: an unpicked slate game is legal, but the submission still has to add up to
    exactly picks_required. Dropping one of the pool's 4 required picks (world's pool sets
    picks_required equal to its 4 game slate) must be rejected with the count message, not
    the old per-game "missing a winner" message that Phase 3 removed.
    """
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    dropped = world["game_ids"][0]
    del data[f"winner-{dropped}"]
    del data[f"confidence-{dropped}"]
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "You have picked 3 games. Pick 4." in response.text


def test_server_rejects_too_many_picks_even_if_no_client_would_send_them(
    client, world, session_factory
):
    """Server side validation is authoritative for the picks_required rule, never the
    client. Lower this pool's picks_required below its slate size, then hand craft a
    submission covering every slate game, exactly what a stale or bypassed client might
    send. It must be rejected, and nothing must be saved.
    """
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.picks_required = 3  # below the world fixture's 4 game slate
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post(
        "/picks", data=_valid_submission(world["game_ids"]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 400
    assert "You have picked 4 games. Pick 3." in response.text

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


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


# Player lock, distinct from the pool wide time lock (Phase 4) -------------


def test_picks_lock_with_a_valid_submission_saves_and_locks(client, world, session_factory):
    _login(client, "player@example.com")
    response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == len(world["game_ids"])
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is not None
    assert entry.locked_at is not None
    assert entry.submitted_at is not None
    db.close()


def test_picks_lock_rejects_an_invalid_submission_and_does_not_lock(client, world, session_factory):
    """The same validation Save runs, and the same refusal: nothing is written, and
    locked_at is never set, whether or not a WeekEntry already existed."""
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    dropped = world["game_ids"][0]
    del data[f"winner-{dropped}"]
    del data[f"confidence-{dropped}"]
    response = client.post("/picks/lock", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "You have picked 3 games. Pick 4." in response.text

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is None or entry.locked_at is None
    db.close()


def test_picks_lock_is_refused_once_the_week_is_time_locked(client, world, session_factory):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post(
        "/picks/lock", data=_valid_submission(world["game_ids"]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 403

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_picks_unlock_clears_the_lock_while_the_week_is_still_open(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks/lock", data=_valid_submission(world["game_ids"]))

    response = client.post("/picks/unlock")
    assert response.status_code == 303

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is None
    # Unlocking never touches the picks themselves.
    assert len(list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))) == 4
    db.close()


def test_picks_unlock_is_refused_once_the_week_is_time_locked(client, world, session_factory):
    """A time locked week can never be unlocked by the player again, no matter what."""
    _login(client, "player@example.com")
    client.post("/picks/lock", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()

    response = client.post("/picks/unlock", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert "locked" in response.text.lower()

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


def test_picks_page_renders_in_every_state(client, world, session_factory):
    """Nothing entered, partial, full but unlocked, player locked, and time locked. The
    real lock always wins over a player lock the moment it hits, regardless of locked_at.
    """
    _login(client, "player@example.com")

    # 1: nothing entered yet.
    response = client.get("/picks")
    assert response.status_code == 200

    # 2: partially entered. Save always requires the full count, so a genuinely partial
    # entry (fewer picks saved than picks_required) is written directly, the same way a
    # commissioner raising picks_required after a smaller entry was already saved would
    # produce one.
    db = session_factory()
    db.add(
        Pick(
            user_id=world["player_id"],
            pool_id=world["pool_id"],
            week_id=world["week_id"],
            game_id=world["game_ids"][0],
            picked_team="home",
            confidence=1,
        )
    )
    db.commit()
    db.close()
    response = client.get("/picks")
    assert response.status_code == 200

    # 3: fully entered, not locked.
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303
    response = client.get("/picks")
    assert response.status_code == 200
    assert "Reorder to inputs" in response.text
    assert "Unlock to edit" not in response.text

    # 4: locked by the player, the week itself still open.
    response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303
    response = client.get("/picks")
    assert response.status_code == 200
    assert "Unlock to edit" in response.text
    assert "Reorder to inputs" not in response.text

    # 5: time locked. The pool wide lock always wins, even though locked_at is still set.
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()
    response = client.get("/picks")
    assert response.status_code == 200
    assert "data-sortable" not in response.text
    assert "Unlock to edit" not in response.text

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


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


# Season/Results split, sortable tables, and the player-major grid ----------


def test_standings_page_has_no_weekly_leaderboard(client, world):
    """Phase 6: /standings is season standings only. The word "leaderboard" and the old
    weekly section's heading id must both be genuinely gone, not just visually hidden.
    """
    _login(client, "player@example.com")
    response = client.get("/standings")
    assert response.status_code == 200
    assert "leaderboard" not in response.text.lower()
    assert "weekly-heading" not in response.text
    assert "Season standings" in response.text


def test_results_weekly_leaderboard_matches_the_selected_week(client, world, session_factory):
    """The weekly leaderboard on /results must reflect whichever week the switcher has
    selected (?week=), not always the latest week in the pool.
    """
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week5 = db.get(Week, world["week_id"])
    week5.status = "locked"
    db.add(
        WeekEntry(
            user_id=world["player_id"],
            pool_id=pool.id,
            week_id=week5.id,
            points=3,
            correct=2,
            possible=4,
            is_winner=True,
            submitted_at=dt.datetime.now(UTC),
        )
    )
    week6 = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=6,
        label="Week 6",
        status="locked",
        lock_at=dt.datetime.now(UTC) - dt.timedelta(hours=1),
    )
    db.add(week6)
    db.flush()
    db.add(
        WeekEntry(
            user_id=world["player_id"],
            pool_id=pool.id,
            week_id=week6.id,
            points=9,
            correct=1,
            possible=4,
            is_winner=True,
            submitted_at=dt.datetime.now(UTC),
        )
    )
    db.commit()
    db.close()

    _login(client, "player@example.com")

    resp5 = client.get("/results?week=5")
    assert resp5.status_code == 200
    assert "Week 5 leaderboard" in resp5.text
    assert 'data-sort-value="3"' in resp5.text
    assert 'data-sort-value="9"' not in resp5.text

    resp6 = client.get("/results?week=6")
    assert resp6.status_code == 200
    assert "Week 6 leaderboard" in resp6.text
    assert 'data-sort-value="9"' in resp6.text
    assert 'data-sort-value="3"' not in resp6.text


def test_results_weekly_leaderboard_stays_private_until_lock(client, world, session_factory):
    """The weekly leaderboard follows the same reveal rule as the pick grid: an entry
    existing at all before lock is itself a "who has submitted" leak.
    """
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    client.post("/logout")

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Regular Player" not in response.text


def test_results_grid_is_player_major_with_confidence_columns_and_game_major_toggle(
    client, session_factory
):
    """Rows are players, columns are confidence values, cells show the matchup, and the
    old game-major table is still present in the response, behind the toggle.
    """
    db = session_factory()
    pool = _make_pool(db, num_games=4, picks_required=4)
    alpha = _make_user(db, "alpha@example.com", "Alpha Player")
    beta = _make_user(db, "beta@example.com", "Beta Player")
    db.add(PoolMember(pool_id=pool.id, user_id=alpha.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=beta.id, role_in_pool="member"))
    week = _make_week(db, pool, status="locked")
    games = _make_games(db, week, count=4)
    db.commit()

    games[0].status, games[0].winner = "final", "home"
    games[0].home_score, games[0].away_score = 21, 14
    games[1].status, games[1].winner = "final", "away"
    games[1].home_score, games[1].away_score = 10, 17
    games[2].status = "void"
    games[3].status = "scheduled"
    db.commit()

    # Alpha picks all four games: game0 correct (home, confidence 4), game1 wrong (home,
    # confidence 3), game2 void (away, confidence 2), game3 not yet final (home, confidence 1).
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[0].id,
            picked_team="home",
            confidence=4,
        )
    )
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[1].id,
            picked_team="home",
            confidence=3,
        )
    )
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[2].id,
            picked_team="away",
            confidence=2,
        )
    )
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[3].id,
            picked_team="home",
            confidence=1,
        )
    )
    # Beta only picks three of the four games, so confidence 1 has nothing mapped to it.
    db.add(
        Pick(
            user_id=beta.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[0].id,
            picked_team="away",
            confidence=4,
        )
    )
    db.add(
        Pick(
            user_id=beta.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[1].id,
            picked_team="away",
            confidence=3,
        )
    )
    db.add(
        Pick(
            user_id=beta.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[2].id,
            picked_team="home",
            confidence=2,
        )
    )
    db.commit()
    db.close()

    _login(client, "alpha@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    text = response.text

    # Player-major: Alpha's confidence 4 column shows the matchup staked there, "H0 over A0",
    # the home team Alpha picked (correct) followed by the opponent, regardless of outcome.
    assert "<strong>H0</strong> over A0" in text
    # Alpha's void pick (confidence 2) carries a clear void marker.
    assert 'pick-void-badge">Void<' in text
    # Beta did not pick a fourth game: confidence 1 renders empty for Beta, not an error.
    assert "No pick at confidence 1" in text

    # The game-major table (rows are games, columns are players) is still present in the
    # response, behind the toggle, even though JS defaults to hiding it.
    assert 'data-view-panel="game"' in text
    assert 'class="pick-player' in text
    assert 'data-view-btn="game"' in text


def test_scoring_end_to_end(client, world, session_factory):
    """Save picks, finalise the games, score, and check the leaderboard math.

    world's pool never sets scoring_mode, so it runs under the real default, "inverse":
    wrong picks count against the player, not right ones. The boss never submits a pick at
    all (see the world fixture), which doubles as the no-show side of Phase 2's central
    rule: a no-show takes the maximum penalty and can never win the week, even though the
    player's own inverse points are not the week's lowest number in isolation.
    """
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))

    # The player picked home, away, home, away with confidence 4, 3, 2, 1.
    # Make the first two correct and the last two wrong. Under inverse only the wrong
    # picks count, against the player: 2 + 1 = 3.
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
    assert entry.points == 3
    assert entry.correct == 2
    assert entry.possible == 4
    assert entry.did_not_submit is False
    assert entry.is_winner is True
    assert report.week_complete is True
    assert db.get(Week, week.id).status == "scored"

    # The boss never submitted a pick. With a 4 game slate the maximum penalty is
    # sum(1..4) = 10, and did_not_submit keeps them out of the running no matter how that
    # compares to the player's own score.
    boss_entry = db.scalar(
        select(WeekEntry).where(WeekEntry.user_id == world["boss_id"], WeekEntry.week_id == week.id)
    )
    assert boss_entry.did_not_submit is True
    assert boss_entry.points == 10
    assert boss_entry.correct == 0
    assert boss_entry.is_winner is False
    db.close()

    response = client.get("/standings")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_scoring_end_to_end_standard_mode_still_works(client, world, session_factory):
    """scoring_mode is switchable per pool without a code change: standard mode must keep
    the pre-Phase-2 behavior exactly, correct picks earn points, highest total wins.
    """
    from app.services.results import score_week_for_pool

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scoring_mode = "standard"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))

    outcomes = ["home", "away", "away", "home"]
    for game, winner in zip(games, outcomes, strict=False):
        game.status = "final"
        game.winner = winner
        game.home_score = 21 if winner == "home" else 17
        game.away_score = 17 if winner == "home" else 21
    db.commit()

    score_week_for_pool(db, pool, week)
    db.commit()

    entry = db.scalar(
        select(WeekEntry).where(
            WeekEntry.user_id == world["player_id"], WeekEntry.week_id == week.id
        )
    )
    assert entry.points == 7
    assert entry.correct == 2
    assert entry.did_not_submit is False
    assert entry.is_winner is True

    boss_entry = db.scalar(
        select(WeekEntry).where(WeekEntry.user_id == world["boss_id"], WeekEntry.week_id == week.id)
    )
    assert boss_entry.did_not_submit is True
    assert boss_entry.points == 0
    db.close()


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
    # Game 1 (conf 4, picked home) is void, so it earns nothing and drops out of possible.
    # Under the pool's default inverse mode, the wrong picks are what count: game 2
    # (away, but home won, conf 3) and game 4 (away, but home won, conf 1): 3 + 1 = 4.
    assert entry.points == 4
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
            "picks_required": "15",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
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
    assert pool.picks_required == 15
    assert pool.timezone == "America/Chicago"
    assert pool.auto_publish is True
    assert pool.open_registration is False
    db.close()


def test_commissioner_cannot_set_picks_required_above_the_slate_size(
    client, world, session_factory
):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "5",  # more than the 4 game slate
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "auto_publish": "1",
            "open_registration": "",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    # Rejected: picks_required is unchanged from the world fixture's original value.
    assert pool.picks_required == 4
    db.close()


def test_commissioner_can_change_the_scenarios_panel_thresholds(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "3",
            "scenarios_min_remaining_games": "2",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.scenarios_min_final_games == 3
    assert pool.scenarios_min_remaining_games == 2
    db.close()


def test_commissioner_cannot_set_scenarios_min_remaining_games_below_one(
    client, world, session_factory
):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "0",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    # Rejected: unchanged from the world fixture's original value (the Pool default, 1).
    assert pool.scenarios_min_remaining_games == 1
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


# Venmo entry gate (Phase 7) ---------------------------------------------------


def _member_row(session_factory, pool_id: int, user_id: int) -> PoolMember:
    db = session_factory()
    member = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool_id, PoolMember.user_id == user_id)
    )
    db.close()
    return member


def test_unpaid_member_cannot_save_picks_when_payment_required(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 403
    assert "pay your entry fee" in response.text.lower()

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_unpaid_member_cannot_lock_picks_when_payment_required(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 403
    assert "pay your entry fee" in response.text.lower()

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is None or entry.locked_at is None
    db.close()


def test_paid_member_can_save_and_lock_picks_when_payment_required(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["player_id"]
        )
    )
    member.paid_at = dt.datetime.now(UTC)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is not None
    db.close()

    lock_response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert lock_response.status_code == 303

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


def test_payment_not_required_lets_anyone_pick_regardless_of_paid_at(
    client, world, session_factory
):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = False
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["player_id"]
        )
    )
    assert member.paid_at is None  # still unpaid, and it must not matter
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    lock_response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert lock_response.status_code == 303


def test_picks_page_shows_the_venmo_panel_when_gated(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    pool.entry_fee = 25.0
    pool.venmo_handle = "poolcollector"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "Pay your entry fee to pick" in response.text
    assert "poolcollector" in response.text
    # The editable, save-and-lock form must not render while gated.
    assert "data-sortable" not in response.text


# Admin: paid toggle, bulk mark paid, duplicate Venmo handle warning ----------


def test_member_paid_toggle_marks_and_unmarks(client, world, session_factory):
    member = _member_row(session_factory, world["pool_id"], world["player_id"])
    _login(client, "boss@example.com")

    response = client.post(f"/admin/members/{member.id}/paid")
    assert response.status_code == 303
    db = session_factory()
    refreshed = db.get(PoolMember, member.id)
    assert refreshed.paid_at is not None
    assert refreshed.paid_marked_by_user_id == world["boss_id"]
    db.close()

    client.post(f"/admin/members/{member.id}/paid")
    db = session_factory()
    refreshed = db.get(PoolMember, member.id)
    assert refreshed.paid_at is None
    assert refreshed.paid_marked_by_user_id is None
    db.close()


def test_members_paid_bulk_marks_only_the_selected_unpaid(client, world, session_factory):
    db = session_factory()
    third = _make_user(db, "third@example.com", "Third Player")
    db.add(PoolMember(pool_id=world["pool_id"], user_id=third.id, role_in_pool="member"))
    db.commit()

    player_member = _member_row(session_factory, world["pool_id"], world["player_id"])
    third_member = _member_row(session_factory, world["pool_id"], third.id)
    boss_member = _member_row(session_factory, world["pool_id"], world["boss_id"])

    boss_member = db.get(PoolMember, boss_member.id)
    boss_member.paid_at = dt.datetime.now(UTC)  # already paid, must be left untouched
    db.commit()
    db.close()

    # Read back through a fresh session, same as the bulk route itself will, so the
    # "untouched" comparison below is not thrown off by SQLite handing datetimes back naive.
    db = session_factory()
    boss_paid_before = db.get(PoolMember, boss_member.id).paid_at
    db.close()

    _login(client, "boss@example.com")
    response = client.post(
        "/admin/members/paid/bulk",
        data={"member_ids": [player_member.id, third_member.id, boss_member.id]},
    )
    assert response.status_code == 303

    db = session_factory()
    assert db.get(PoolMember, player_member.id).paid_at is not None
    assert db.get(PoolMember, third_member.id).paid_at is not None
    assert db.get(PoolMember, boss_member.id).paid_at == boss_paid_before
    db.close()


def test_duplicate_venmo_handle_warning_fires_for_shared_handle_not_for_different_or_empty(
    client, world, session_factory
):
    player_member = _member_row(session_factory, world["pool_id"], world["player_id"])
    boss_member = _member_row(session_factory, world["pool_id"], world["boss_id"])

    _login(client, "boss@example.com")

    # Different, non empty handles: no warning for either.
    client.post(
        f"/admin/members/{player_member.id}/venmo-handle",
        data={"member_venmo_handle": "player-handle"},
    )
    client.post(
        f"/admin/members/{boss_member.id}/venmo-handle",
        data={"member_venmo_handle": "boss-handle"},
    )
    response = client.get("/admin/members")
    assert "Possible duplicate account" not in response.text

    # Both empty: no warning either (an empty handle is not a shared account).
    client.post(f"/admin/members/{player_member.id}/venmo-handle", data={"member_venmo_handle": ""})
    client.post(f"/admin/members/{boss_member.id}/venmo-handle", data={"member_venmo_handle": ""})
    response = client.get("/admin/members")
    assert "Possible duplicate account" not in response.text

    # Same handle (case and whitespace variations): both rows flagged.
    client.post(
        f"/admin/members/{player_member.id}/venmo-handle",
        data={"member_venmo_handle": "SharedHandle"},
    )
    client.post(
        f"/admin/members/{boss_member.id}/venmo-handle",
        data={"member_venmo_handle": " sharedhandle "},
    )
    response = client.get("/admin/members")
    assert response.text.count("Possible duplicate account") == 2


# Admin: settings save, payout rules, and the pot validator -------------------


def test_settings_save_persists_venmo_and_payment_fields(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
            "entry_fee": "25.50",
            "venmo_handle": "the-collector",
            "payment_required_to_pick": "1",
            "payment_note": "Note as Week 1 in the Venmo app.",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.entry_fee == 25.50
    assert pool.venmo_handle == "the-collector"
    assert pool.payment_required_to_pick is True
    assert pool.payment_note == "Note as Week 1 in the Venmo app."
    db.close()


def test_settings_save_rejects_a_negative_entry_fee(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
            "entry_fee": "-5",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.entry_fee is None  # rejected, unchanged from the world fixture's default
    db.close()


def test_payout_rule_add_and_remove(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "amount": "100.00", "label": "1st place"},
    )
    assert response.status_code == 303

    db = session_factory()
    rule = db.scalar(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"]))
    assert rule is not None
    assert rule.scope == "weekly"
    assert rule.place == 1
    assert rule.amount == 100.0
    assert rule.label == "1st place"
    rule_id = rule.id
    db.close()

    settings_response = client.get("/admin/settings")
    assert "100" in settings_response.text

    remove_response = client.post(f"/admin/payouts/rule/{rule_id}/remove")
    assert remove_response.status_code == 303

    db = session_factory()
    assert db.get(PayoutRule, rule_id) is None
    db.close()


def test_pot_validator_warns_on_mismatch_and_not_on_match(client, world, session_factory):
    _login(client, "boss@example.com")

    # Nobody paid, no rules: 0 collected, 0 allocated, balanced.
    response = client.get("/admin/settings")
    assert "does not match" not in response.text

    # Set a fee, mark one member paid, but write no payout rules: now mismatched.
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.entry_fee = 20.0
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["player_id"]
        )
    )
    member.paid_at = dt.datetime.now(UTC)
    db.commit()
    db.close()

    mismatched = client.get("/admin/settings")
    assert "does not match" in mismatched.text

    # Write a payout rule for exactly the collected amount: balanced again. Saving settings
    # is unaffected either way, matching every response above returning 200.
    client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "amount": "20.00", "label": ""},
    )
    matched = client.get("/admin/settings")
    assert "does not match" not in matched.text


# Results: the weekly payout column --------------------------------------------


def _score_worlds_only_week(client, session_factory, world):
    """Submit the player's picks, finalise every game, and score the week, matching the
    exact fixture test_scoring_end_to_end uses (player's inverse points come out to 3)."""
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))
    outcomes = ["home", "away", "away", "home"]
    for game, winner in zip(games, outcomes, strict=False):
        game.status = "final"
        game.winner = winner
        game.home_score = 21 if winner == "home" else 17
        game.away_score = 17 if winner == "home" else 21
    db.commit()

    score_week_for_pool(db, pool, week)
    db.commit()
    db.close()


def test_results_no_payout_rules_means_no_payout_column(client, world, session_factory):
    _score_worlds_only_week(client, session_factory, world)

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Payout" not in response.text


def test_results_weekly_payout_column_matches_ranks(client, world, session_factory):
    """Under inverse mode the player (some wrong picks, 3 points against) outranks the boss
    (a no show, the maximum penalty for a 4 game slate, 10 points against): the player is
    rank 1, the boss is rank 2."""
    _score_worlds_only_week(client, session_factory, world)

    db = session_factory()
    db.add(PayoutRule(pool_id=world["pool_id"], scope="weekly", place=1, amount=100.0))
    db.add(PayoutRule(pool_id=world["pool_id"], scope="weekly", place=2, amount=40.0))
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Payout" in response.text
    assert "100 dollars" in response.text
    assert "40 dollars" in response.text


def test_results_bowl_week_payout_uses_bowl_scope_not_weekly(client, world, session_factory):
    _score_worlds_only_week(client, session_factory, world)

    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.is_bowl_week = True
    db.add(PayoutRule(pool_id=world["pool_id"], scope="weekly", place=1, amount=999.0))
    db.add(PayoutRule(pool_id=world["pool_id"], scope="bowl", place=1, amount=55.0))
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "55 dollars" in response.text
    assert "999 dollars" not in response.text


# Results: the scenarios panel and build-your-own-scenario (Phase 8) -----------


def _lock_worlds_week(session_factory, world):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    db.commit()
    db.close()


def test_scenarios_panel_shows_pending_state_below_the_threshold(client, world, session_factory):
    """The world fixture's pool keeps the Pool model default thresholds (5 final games, 1
    remaining) and only has 4 games total, none final, so the panel must show the pending
    copy naming the real configured number, never a hard coded "five"."""
    _lock_worlds_week(session_factory, world)
    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Scenarios open once 5 games are final" in response.text


def test_scenarios_panel_visible_shows_placement_percentages(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    _login(client, "boss@example.com")

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scenarios_min_final_games = 1
    pool.scenarios_min_remaining_games = 1
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))
    # Finalise only the first game, leaving the other three remaining.
    games[0].status = "final"
    games[0].winner = "home"
    games[0].home_score = 21
    games[0].away_score = 17
    db.commit()
    db.close()

    response = client.get("/results")
    assert response.status_code == 200
    assert "Scenarios open once" not in response.text
    assert "1st" in response.text and "2nd" in response.text and "3rd" in response.text
    assert "Build your own scenario" in response.text


def test_scenarios_panel_moneyline_toggle_labels_estimated_from_betting_odds(
    client, world, session_factory
):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    _login(client, "boss@example.com")

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scenarios_min_final_games = 0
    pool.scenarios_min_remaining_games = 1
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    db.commit()
    db.close()

    even = client.get("/results")
    assert "Estimated from betting odds" not in even.text

    moneyline = client.get("/results?model=moneyline")
    assert "Estimated from betting odds" in moneyline.text


def test_custom_scenario_endpoint_recomputes_standings_for_every_player(
    client, world, session_factory
):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    _lock_worlds_week(session_factory, world)
    _login(client, "boss@example.com")

    db = session_factory()
    game_id = world["game_ids"][0]
    db.close()

    response = client.post(
        "/results/custom-scenario", data={"week": "5", f"game_{game_id}": "home"}
    )
    assert response.status_code == 200
    assert "Regular Player" in response.text
    assert "The Commissioner" in response.text
    assert "Assuming" in response.text


def test_custom_scenario_endpoint_blocked_before_the_week_locks(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post("/results/custom-scenario", data={"week": "5"})
    assert response.status_code == 403


# Season standings: the awards panel -------------------------------------------


def test_season_awards_panel_absent_before_any_week_is_scored(client, world):
    db_login = "boss@example.com"
    _login(client, db_login)
    response = client.get("/standings")
    assert response.status_code == 200
    assert "Season awards" not in response.text


def test_season_awards_panel_shows_amounts_once_a_week_is_scored(client, world, session_factory):
    _score_worlds_only_week(client, session_factory, world)

    db = session_factory()
    db.add(PayoutRule(pool_id=world["pool_id"], scope="season", place=1, amount=50.0))
    db.add(PayoutRule(pool_id=world["pool_id"], scope="season", place=2, amount=20.0))
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/standings")
    assert response.status_code == 200
    assert "Season awards" in response.text
    assert "50 dollars" in response.text
    assert "20 dollars" in response.text
