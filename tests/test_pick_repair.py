"""app/services/pick_repair.py: the repair side of the orphaned picks incident (audit_week/
audit_pool are covered in tests/test_orphaned_picks.py; this covers plan_and_apply_repair,
ensure_unique_constraint, and the repair-picks CLI command). See DECISIONS.md, "Orphaned
picks", for the keep rule and every other judgment call.

The centerpiece here reproduces the commissioner's own reported case exactly: a player with
16 picks against a 15-pick requirement, confidence values 1, 3, 4, 4, 5, 6, 7, 7, 8, 9, 10,
11, 12, 13, 14, 15 (2 missing, 4 and 7 each used twice), reading MICH 7 / TEX 1 / BUF 3 /
PIT 7 as wins, GB 4 / DAL 4 / ORE 14 as losses, the rest wins. Under inverse scoring that is
22 points against (4 + 4 + 14) and 13 correct. The keep rule must drop exactly one pick and
land the player on 15 picks and 18 points against; landing on any other number means the keep
rule itself is wrong, per the incident brief.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.models import (
    Base,
    Game,
    Pick,
    PickArchive,
    Pool,
    PoolMember,
    User,
    Week,
    WeekEntry,
)
from app.services.pick_repair import ensure_unique_constraint, plan_and_apply_repair

UTC = dt.UTC


@pytest.fixture
def engine():
    """A schema built WITHOUT uq_pick_user_week_confidence, standing in for a production
    database as it exists before repair-picks (and, if the data ends up clean,
    ensure_unique_constraint) actually add it. The fixtures below need to insert picks that
    already violate that constraint (that is the whole incident: real corrupt rows that
    predate it), which a schema built with the constraint already in place would refuse to
    hold at all, this table's own protection working exactly as intended. Temporarily pulled
    from Pick.__table__.constraints for create_all and restored immediately after, since
    Base.metadata is shared process-wide and other test modules assume it is intact.
    """
    named = {c.name for c in Pick.__table__.constraints}
    assert "uq_pick_user_week_confidence" in named
    removed = {c for c in Pick.__table__.constraints if c.name == "uq_pick_user_week_confidence"}
    for c in removed:
        Pick.__table__.constraints.discard(c)

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
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


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Repair Test Pool",
        "join_code": "REPAIRTEST",
        "season_year": 2025,
        "num_games_per_week": 16,
        "target_nfl": 8,
        "target_ncaaf": 8,
        "picks_required": 15,
        "sports": ["nfl", "ncaaf"],
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


def _week(db, pool, *, status="scored") -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=1,
        label="Week 1",
        status=status,
        lock_at=dt.datetime.now(UTC) - dt.timedelta(days=1),
    )
    db.add(week)
    db.flush()
    return week


def _game(db, week, abbr, status="final", winner="home", rank=1) -> Game:
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id=f"repair-{abbr}-{week.id}",
        start_time=dt.datetime.now(UTC) - dt.timedelta(days=2),
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


@pytest.fixture
def reported_incident(session_factory):
    """Reproduces the commissioner's exact reported case: 16 picks, 15 required, values
    1,3,4,4,5,6,7,7,8,9,10,11,12,13,14,15 (2 missing, 4 and 7 each twice). All 16 picks share
    one updated_at (one save overwrote almost everything), so DAL, created last, sorts last
    among the four rows tied on the duplicate-value tiebreak and is the one dropped."""
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner")
    player = _user(db, "player@example.com", "Flagged Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")

    # (abbr, confidence, winner side for a "home" pick: "home" = win, "away" = win means the
    # player's home pick loses). All picks below are picked_team="home", so the game's own
    # winner decides right/wrong: winner="home" -> correct, winner="away" -> the player's
    # pick loses.
    layout = [
        ("TEX", 1, "home"),  # win
        ("BUF", 3, "home"),  # win
        ("GB", 4, "away"),  # loss
        ("G5", 5, "home"),  # win
        ("G6", 6, "home"),  # win
        ("MICH", 7, "home"),  # win
        ("PIT", 7, "home"),  # win
        ("G8", 8, "home"),  # win
        ("G9", 9, "home"),  # win
        ("G10", 10, "home"),  # win
        ("G11", 11, "home"),  # win
        ("G12", 12, "home"),  # win
        ("G13", 13, "home"),  # win
        ("ORE", 14, "away"),  # loss
        ("G15", 15, "home"),  # win
        ("DAL", 4, "away"),  # loss, created last -> highest game_id among the two 4s
    ]
    now = dt.datetime.now(UTC)
    games_by_abbr = {}
    for index, (abbr, confidence, winner) in enumerate(layout):
        game = _game(db, week, abbr, winner=winner, rank=index + 1)
        games_by_abbr[abbr] = game
        db.add(
            Pick(
                user_id=player.id,
                pool_id=pool.id,
                week_id=week.id,
                game_id=game.id,
                picked_team="home",
                confidence=confidence,
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()
    data = {
        "pool_id": pool.id,
        "week_id": week.id,
        "player_id": player.id,
        "games_by_abbr": {abbr: g.id for abbr, g in games_by_abbr.items()},
    }
    db.close()
    return data


def test_the_reported_player_lands_on_15_picks_and_18_points(reported_incident, session_factory):
    """The concrete verification target from the incident brief: 16 -> 15 picks, 22 -> 18
    points against, 13 correct throughout. Landing on any other number means the keep rule
    itself is wrong."""
    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])

    plans = plan_and_apply_repair(db, pool, skip_paid=True)
    assert len(plans) == 1
    plan = plans[0]
    assert len(plan.players) == 1
    result = plan.players[0]

    assert result.before_pick_count == 16
    assert result.after_pick_count == 15
    assert result.before_points == 22
    assert result.after_points == 18
    assert result.before_correct == 13
    assert result.after_correct == 13

    # The specific orphan removed must be the DAL loss at confidence 4, not the GB one: DAL
    # was created last and both 4s are equally "duplicated", so game_id is the deciding
    # tiebreak (see the keep rule in app/services/pick_repair.py).
    assert len(result.orphans) == 1
    orphan = result.orphans[0]
    assert orphan.confidence == 4
    assert orphan.game_id == reported_incident["games_by_abbr"]["DAL"]

    # The kept set still is not a clean permutation (duplicate 7, missing 2): flagged, never
    # renumbered.
    assert result.still_not_clean is True
    assert result.remaining_duplicate_values == [7]
    assert result.remaining_missing_values == [2]

    db.rollback()
    db.close()


def test_dry_run_writes_nothing(reported_incident, session_factory):
    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])
    plan_and_apply_repair(db, pool, skip_paid=True)
    db.rollback()  # the dry run: compute, then roll back, exactly as repair-picks does
    db.close()

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == reported_incident["player_id"])))
    assert len(picks) == 16, "a dry run must never leave the repair committed"
    archived = list(db.scalars(select(PickArchive)))
    assert archived == []
    db.close()


def test_apply_archives_and_deletes_the_orphan_and_rescoring_matches(
    reported_incident, session_factory
):
    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])
    plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()  # the real --apply path
    db.close()

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == reported_incident["player_id"])))
    assert len(picks) == 15
    dal_id = reported_incident["games_by_abbr"]["DAL"]
    assert dal_id not in {p.game_id for p in picks}

    archived = list(
        db.scalars(select(PickArchive).where(PickArchive.user_id == reported_incident["player_id"]))
    )
    assert len(archived) == 1
    assert archived[0].game_id == dal_id
    assert archived[0].confidence == 4
    assert archived[0].reason == "orphan_repair"

    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == reported_incident["player_id"]))
    assert entry.points == 18
    assert entry.correct == 13
    assert entry.possible == 15
    db.close()


def test_a_clean_player_is_left_untouched(reported_incident, session_factory):
    """A second, clean player (exactly picks_required picks, a real permutation) in the same
    pool and week must not be touched by a repair triggered by someone else's corruption."""
    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])
    week = db.get(Week, reported_incident["week_id"])
    clean_user = _user(db, "clean@example.com", "Clean Player")
    db.add(PoolMember(pool_id=pool.id, user_id=clean_user.id, role_in_pool="member"))
    games = list(db.scalars(select(Game).where(Game.week_id == week.id)))[:15]
    now = dt.datetime.now(UTC)
    for i, game in enumerate(games):
        db.add(
            Pick(
                user_id=clean_user.id,
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

    plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()

    picks = list(db.scalars(select(Pick).where(Pick.user_id == clean_user.id)))
    assert len(picks) == 15
    assert sorted(p.confidence for p in picks) == list(range(1, 16))
    db.close()


def test_payout_difference_is_reported_and_not_silently_applied(reported_incident, session_factory):
    """A week whose weekly payout is already frozen but not yet paid: the repair still runs
    (nothing has been paid over Venmo yet) and any award difference is reported via
    payout_deltas, never written back as a new frozen amount (repair-picks never calls
    recalculate_awards; only a commissioner using the existing /league/slate refresh flow
    does that, deliberately, per app/services/results.py)."""
    from app.models import PayoutAward

    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])
    week = db.get(Week, reported_incident["week_id"])
    week.status = "scored"
    db.add(
        PayoutAward(
            pool_id=pool.id,
            user_id=reported_incident["player_id"],
            scope="weekly",
            week_id=week.id,
            place=3,
            tied_with=1,
            amount=Decimal("10.00"),
            pot_at_award=Decimal("100.00"),
            rule_mode="amount",
            rule_value=Decimal("10.00"),
            awarded_at=dt.datetime.now(UTC),
        )
    )
    db.commit()

    plans = plan_and_apply_repair(db, pool, skip_paid=True)
    plan = plans[0]

    # The award row itself must be untouched by the mere act of computing/writing the repair.
    frozen = db.scalar(
        select(PayoutAward).where(
            PayoutAward.pool_id == pool.id, PayoutAward.user_id == reported_incident["player_id"]
        )
    )
    assert frozen.amount == Decimal("10.00")
    assert frozen.place == 3

    # But since correcting the score can change standings, a difference may be reported.
    for delta in plan.payout_deltas:
        assert delta.scope in ("weekly",)
    db.rollback()
    db.close()


def test_a_paid_award_blocks_the_repair_entirely(reported_incident, session_factory):
    """The hard line: once a weekly award has paid_at set, repair-picks must not touch that
    week's picks at all, dry run or apply, and must report it as skipped for the
    commissioner instead."""
    from app.models import PayoutAward

    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])
    week = db.get(Week, reported_incident["week_id"])
    week.status = "scored"
    db.add(
        PayoutAward(
            pool_id=pool.id,
            user_id=reported_incident["player_id"],
            scope="weekly",
            week_id=week.id,
            place=3,
            tied_with=1,
            amount=Decimal("10.00"),
            pot_at_award=Decimal("100.00"),
            rule_mode="amount",
            rule_value=Decimal("10.00"),
            awarded_at=dt.datetime.now(UTC),
            paid_at=dt.datetime.now(UTC),
        )
    )
    db.commit()

    plans = plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()
    assert len(plans) == 1
    assert plans[0].skipped_paid_scopes == ["weekly"]
    assert plans[0].players == []

    picks = list(db.scalars(select(Pick).where(Pick.user_id == reported_incident["player_id"])))
    assert len(picks) == 16, "a paid week must never be touched"
    db.close()


def test_ensure_unique_constraint_still_blocked_while_the_flagged_player_remains_unclean(
    reported_incident, session_factory, engine
):
    """The reported player's own kept set never becomes clean (duplicate 7, missing 2, see
    the centerpiece test above), so the constraint correctly refuses to add itself pool wide
    even after the repair runs: it would be lying about data that is still not a real
    permutation for that one player."""
    db = session_factory()
    pool = db.get(Pool, reported_incident["pool_id"])
    plan_and_apply_repair(db, pool, skip_paid=True)
    db.commit()
    db.close()

    status = ensure_unique_constraint(engine)
    assert status.startswith("still blocked")


def test_ensure_unique_constraint_adds_it_once_data_is_fully_clean(session_factory, engine):
    """A simpler, fully resolvable case: one true orphan (an old save's leftover row on a
    deselected game, colliding in value with a game from the latest save) and nothing else
    wrong. Once repair-picks removes it, every remaining row across the whole table really is
    a clean permutation, and the constraint can finally be added for real."""
    db = session_factory()
    pool = _pool(db, num_games_per_week=5, picks_required=4)
    player = _user(db, "clean-after-repair@example.com", "Almost Clean Player")
    boss = _user(db, "boss2@example.com", "Boss Two")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _week(db, pool, status="open")

    older = dt.datetime.now(UTC) - dt.timedelta(hours=1)
    newer = dt.datetime.now(UTC)
    orphan_game = _game(db, week, "OLD", winner="home", rank=1)
    kept_games = [_game(db, week, f"NEW{i}", winner="home", rank=i + 2) for i in range(4)]

    db.add(
        Pick(
            user_id=player.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=orphan_game.id,
            picked_team="home",
            confidence=2,
            created_at=older,
            updated_at=older,
        )
    )
    for i, game in enumerate(kept_games):
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
    db.close()

    status = ensure_unique_constraint(engine)
    assert status == "added"
    # Idempotent: calling it again reports already present rather than trying twice.
    assert ensure_unique_constraint(engine) == "already present"
