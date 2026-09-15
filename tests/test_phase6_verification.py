"""Phase 6 full verification for the orphaned picks incident: the automated boot check, the
"save 15 five times in a row, still exactly 15" and "lock, unlock, change, save, still 15"
manual checks turned into real tests, and the four adversarial POSTs the incident brief lists
by number (19-22). See PICKS-REPAIR-REPORT.md for the full Phase 6 checklist including the
items that need a real browser and are not automatable here (360/768/1280px layout, visible
focus rings), and DECISIONS.md, "Orphaned picks", for the incident writeup.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import typer
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, Game, Pick, Pool, PoolMember, User, Week

UTC = dt.UTC


@pytest.fixture
def engine():
    """Built without uq_pick_user_week_confidence: a few tests below insert already-corrupt
    rows sharing a confidence value, standing in for data that predates the fix (see
    tests/test_pick_repair.py's own engine fixture for the same reasoning)."""
    removed = {c for c in Pick.__table__.constraints if c.name == "uq_pick_user_week_confidence"}
    for c in removed:
        Pick.__table__.constraints.discard(c)
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    try:
        Base.metadata.create_all(eng)
    finally:
        Pick.__table__.constraints.update(removed)
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
        "name": "Phase 6 Pool",
        "join_code": "PHASE6TEST",
        "season_year": 2025,
        "num_games_per_week": 8,
        "target_nfl": 4,
        "target_ncaaf": 4,
        "picks_required": 4,
        "sports": ["nfl", "ncaaf"],
        "auto_publish": True,
        "open_registration": False,
        "timezone": "America/New_York",
        "current_week": 1,
        "payment_required_to_pick": False,
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


def _week(db, pool, *, lock_in_hours=48.0, status="open") -> Week:
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


def _games(db, week, count=8) -> list[Game]:
    games = []
    base = dt.datetime.now(UTC) + dt.timedelta(hours=48)
    for i in range(count):
        game = Game(
            week_id=week.id,
            league="nfl" if i % 2 == 0 else "ncaaf",
            espn_event_id=f"p6-evt{i}",
            start_time=base + dt.timedelta(hours=i),
            home_team=f"Home{i}",
            away_team=f"Away{i}",
            home_abbr=f"H{i}",
            away_abbr=f"A{i}",
            canonical_home_key=f"nfl:p6-home-{i}",
            canonical_away_key=f"nfl:p6-away-{i}",
            spread_home=-1.5,
            closeness=1.5,
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
    """8 slate games, picks_required=4: room to genuinely swap which games are picked."""
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner")
    player = _user(db, "player@example.com", "Regular Player")
    outsider = _user(db, "outsider@example.com", "Outsider")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool)
    games = _games(db, week)
    db.commit()
    data = {
        "pool_id": pool.id,
        "week_id": week.id,
        "player_id": player.id,
        "outsider_id": outsider.id,
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
    data = {}
    for index, gid in enumerate(game_ids[:4]):
        data[f"winner-{gid}"] = "home" if index % 2 == 0 else "away"
        data[f"confidence-{gid}"] = str(4 - index)
    return data


# Automated item 5: boot and confirm 200 on every core page. -----------------


def test_boot_returns_200_on_every_core_page(client, world):
    _login(client, "player@example.com")
    client.post("/picks", data=_submission(world["game_ids"][0:4]))
    for path in ("/picks", "/standings", "/results"):
        response = client.get(path)
        assert response.status_code in (200, 303), f"{path} returned {response.status_code}"

    _login(client, "boss@example.com")
    for path in ("/league", "/league/slate"):
        response = client.get(path)
        assert response.status_code in (200, 303), f"{path} returned {response.status_code}"


# Manual item 8: five different combinations in a row, still exactly 4 (this pool's
# picks_required) every single time. -----------------------------------------


def test_five_different_combinations_in_a_row_always_leave_exactly_n_rows(
    client, world, session_factory
):
    game_ids = world["game_ids"]
    _login(client, "player@example.com")
    combinations = [
        game_ids[0:4],
        game_ids[1:5],
        game_ids[2:6],
        game_ids[4:8],
        [game_ids[0], game_ids[3], game_ids[5], game_ids[7]],
    ]
    for combo in combinations:
        response = client.post("/picks", data=_submission(combo))
        assert response.status_code == 303
        db = session_factory()
        picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
        assert len(picks) == 4, f"expected 4 rows after saving {combo}, found {len(picks)}"
        assert {p.game_id for p in picks} == set(combo)
        db.close()


# Manual item 9: lock, unlock, change, save, still exactly n. ----------------


def test_lock_unlock_change_save_still_leaves_exactly_n_rows(client, world, session_factory):
    game_ids = world["game_ids"]
    _login(client, "player@example.com")

    assert client.post("/picks/lock", data=_submission(game_ids[0:4])).status_code == 303
    assert client.post("/picks/unlock").status_code == 303
    response = client.post("/picks", data=_submission(game_ids[3:7]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == 4
    assert {p.game_id for p in picks} == set(game_ids[3:7])
    db.close()


# Adversarial 19: POST picks for a week that is locked. ----------------------


def test_adversarial_post_to_a_locked_week_is_rejected(client, world, session_factory):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.status = "locked"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_submission(world["game_ids"][0:4]))
    assert response.status_code == 403


# Adversarial 20: POST picks as a non-member. 403. ---------------------------


def test_adversarial_post_as_a_non_member_is_refused(client, world):
    _login(client, "outsider@example.com")
    response = client.post("/picks", data=_submission(world["game_ids"][0:4]))
    # The outsider has no active pool at all, so get_active_pool's own 403 applies (see
    # app/routers/picks.py), the same authoritative gate every other picks route depends on.
    assert response.status_code == 403


# Adversarial 21: POST two picks with the same confidence value. Rejected. --


def test_adversarial_duplicate_confidence_value_is_rejected(client, world, session_factory):
    game_ids = world["game_ids"]
    _login(client, "player@example.com")
    data = _submission(game_ids[0:4])
    data[f"confidence-{game_ids[1]}"] = data[f"confidence-{game_ids[0]}"]
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "used twice" in response.text

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


# Adversarial 22: POST a pick for a game on another league's slate. Rejected. -


def test_adversarial_pick_for_a_game_on_another_pools_slate_is_rejected(
    client, world, session_factory
):
    db = session_factory()
    other_pool = _pool(db, name="Other League", join_code="OTHERLEAGUE")
    other_week = _week(db, other_pool)
    other_games = _games(db, other_week, count=4)
    db.commit()
    foreign_game_id = other_games[0].id
    db.close()

    _login(client, "player@example.com")
    data = _submission(world["game_ids"][0:3])
    data[f"winner-{foreign_game_id}"] = "home"
    data[f"confidence-{foreign_game_id}"] = "4"
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


# Automated item 3: no float( in money paths. -------------------------------


def test_no_float_in_money_paths():
    """Money is Decimal end to end (SPEC.md Section 3h). Scans the same files a human review
    of this incident's payout-touching code would: the payout engine, the payout service, the
    payout routes, and this incident's own new repair module."""
    import re

    money_paths = [
        pathlib.Path("app/payouts.py"),
        pathlib.Path("app/services/payouts.py"),
        pathlib.Path("app/routers/payouts.py"),
        pathlib.Path("app/services/pick_repair.py"),
    ]
    pattern = re.compile(r"\bfloat\(")
    for path in money_paths:
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), f"float( found in a money path: {path}"


# Automated item 4: migration up, down, up on a scratch database, clean. ----


def test_migration_up_down_up_on_a_scratch_database(tmp_path):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "phase6_migration_scratch.db"
    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")


# Phase 3 tests the spec explicitly calls for, made explicit and separate from the
# centerpiece "reported incident" test in tests/test_pick_repair.py. -----------


def test_archived_pick_round_trips_every_field(session_factory):
    from app.models import PickArchive
    from app.services.pick_repair import plan_and_apply_repair

    db = session_factory()
    pool = _pool(db, num_games_per_week=5, picks_required=4)
    player = _user(db, "roundtrip@example.com", "Round Trip Player")
    boss = _user(db, "boss3@example.com", "Boss Three")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool)
    games = _games(db, week, count=5)
    older = dt.datetime.now(UTC) - dt.timedelta(hours=1)
    newer = dt.datetime.now(UTC)
    db.add(
        Pick(
            user_id=player.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[0].id,
            picked_team="away",
            confidence=2,
            created_at=older,
            updated_at=older,
        )
    )
    for i, game in enumerate(games[1:]):
        db.add(
            Pick(
                user_id=player.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=i + 1,
                created_at=newer,
                updated_at=newer,
            )
        )
    db.commit()

    plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()

    archived = db.scalar(
        select(PickArchive).where(
            PickArchive.user_id == player.id, PickArchive.game_id == games[0].id
        )
    )
    assert archived is not None
    assert archived.picked_team == "away"
    assert archived.confidence == 2
    assert archived.reason == "orphan_repair"
    stored = archived.original_submitted_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    assert stored == older
    db.close()


def test_tie_break_prefers_non_duplicated_value_then_lower_game_id(session_factory):
    """Every pick shares one updated_at (a full tie on recency): the keep rule must then
    prefer a pick whose confidence is not duplicated elsewhere, and among equally-duplicated
    picks, the lower game_id."""
    from app.services.pick_repair import plan_and_apply_repair

    db = session_factory()
    pool = _pool(db, num_games_per_week=5, picks_required=4)
    player = _user(db, "tiebreak@example.com", "Tiebreak Player")
    boss = _user(db, "boss4@example.com", "Boss Four")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool)
    games = _games(db, week, count=5)  # ids assigned in creation order, ascending
    now = dt.datetime.now(UTC)
    # values: 1, 2, 2, 3, 4 -- duplicate at value 2 (games[1], games[2]). Everything ties on
    # updated_at, so the non-duplicated values (1, 3, 4) are always kept; between the two 2s,
    # the lower game_id (games[1]) is kept and the higher (games[2]) is the orphan.
    values = [1, 2, 2, 3, 4]
    for game, value in zip(games, values, strict=True):
        db.add(
            Pick(
                user_id=player.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=value,
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()

    plans = plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()

    result = plans[0].players[0]
    assert len(result.orphans) == 1
    assert result.orphans[0].game_id == games[2].id
    assert result.orphans[0].confidence == 2
    db.close()


def test_repair_recomputed_score_matches_a_hand_calculation(session_factory):
    """A simple, independent hand-calculated case distinct from the reported incident: 5
    picks (values 1..5) against a 4-pick requirement, dropping the oldest (value 5, a loss).
    Kept: 1 (win), 2 (win), 3 (loss), 4 (win). Inverse points against = 3 (the one kept loss).
    """
    from app.services.pick_repair import plan_and_apply_repair

    db = session_factory()
    pool = _pool(db, num_games_per_week=5, picks_required=4)
    player = _user(db, "handcalc@example.com", "Hand Calc Player")
    boss = _user(db, "boss5@example.com", "Boss Five")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")
    games = _games(db, week, count=5)
    winners = ["home", "home", "away", "home", "away"]  # picks are all "home"
    for game, winner in zip(games, winners, strict=True):
        game.status = "final"
        game.winner = winner
    older = dt.datetime.now(UTC) - dt.timedelta(hours=1)
    newer = dt.datetime.now(UTC)
    for i, game in enumerate(games):
        db.add(
            Pick(
                user_id=player.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=i + 1,
                created_at=older if i == 4 else newer,
                updated_at=older if i == 4 else newer,
            )
        )
    db.commit()

    plans = plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()

    result = plans[0].players[0]
    assert result.orphans[0].confidence == 5
    assert result.after_points == 3
    assert result.after_correct == 3
    db.close()


def test_doctor_picks_cli_reports_the_affected_count(session_factory, capsys, monkeypatch):
    from app.cli import doctor_picks_cmd

    monkeypatch.setattr("app.db.SessionLocal", session_factory)

    db = session_factory()
    pool = _pool(db, num_games_per_week=5, picks_required=4)
    player = _user(db, "clidoctor@example.com", "CLI Doctor Player")
    boss = _user(db, "boss6@example.com", "Boss Six")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool)
    games = _games(db, week, count=5)
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

    with pytest.raises(typer.Exit):
        doctor_picks_cmd(pool_id=None)
    out = capsys.readouterr().out
    assert "1 affected" in out
    assert "CLI Doctor Player" in out


def test_doctor_picks_cli_reports_clean_when_nothing_is_affected(
    session_factory, capsys, monkeypatch
):
    from app.cli import doctor_picks_cmd

    monkeypatch.setattr("app.db.SessionLocal", session_factory)

    db = session_factory()
    pool = _pool(db, num_games_per_week=4, picks_required=4)
    player = _user(db, "clidoctorclean@example.com", "CLI Doctor Clean Player")
    boss = _user(db, "boss7@example.com", "Boss Seven")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool)
    games = _games(db, week, count=4)
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

    doctor_picks_cmd(pool_id=None)  # no typer.Exit: nothing affected
    out = capsys.readouterr().out
    assert "0 affected" in out


def test_keyboard_pick_navigation_js_suite_passes():
    """Phase 2's own check: "Confirm the Tab and arrow key navigation built previously still
    works with the cap in place." app.js was not touched by this incident (see DECISIONS.md),
    so this simply re-confirms the existing Node test suite (tests/js/pick_navigation.test.js
    and friends) is still green, run from within the Python suite so a full `pytest -q` alone
    proves it rather than requiring a separate `npm test` step to be remembered.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed in this environment")
    root = pathlib.Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node, "--test", "tests/js/pick_navigation.test.js"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
