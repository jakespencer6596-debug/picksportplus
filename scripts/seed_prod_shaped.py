"""Build a local, production-shaped sqlite database for this build's own testing.

Not a copy of production, which this build never touches (see the "Production is off
limits" rules the whole standings-and-ties prompt runs under). This reuses the existing,
already-realistic seed-demo pool (real teams, real historical scores, real players,
app/services/demo.py) as its base, then layers on top every messy state known to exist in
the real production database, so Phase 10's local browser pass and Phase 12's migration
safety check both have something real to exercise:

  1. A player with 16 pick rows for one week (one too many), with a duplicate confidence
     value: the orphaned-picks shape (see PICKS-REPAIR-REPORT.md).
  2. Phantom 120-point no-show WeekEntry rows on a week whose lock_at is still in the
     future: the 120 point bug this build's Phase 1 fixes (see DECISIONS.md).
  3. A PayoutAward frozen under the old submission-time tiebreak, now stale against the new
     rule: exercises the "Awarded under the previous tie rule" label (Phase 2 item 5).
  4. A tied week: two players sharing identical points and identical weekly wins entering
     that week, so the wins-then-split rule actually has to split a pot.
  5. A voided game (already present in the demo's own week 1: see app/services/demo.py).
  6. A test week (Week.is_test_week=True, week_number=0), quarantined from the season.
  7. A paid pool member and an unpaid one (the demo marks everyone paid by default; this
     script flips one back to unpaid).

Usage:
    python scripts/seed_prod_shaped.py [--db-path PATH] [--reset]

--db-path defaults to ./picksportplus_prod_shaped.db (the repo's own working directory, a
local sqlite file, never anything remote: this script refuses to run against anything else,
the same rule app/main.py's own boot-time guard enforces for the app itself). --reset drops
the file first and rebuilds from scratch; without it, the script refuses to run against an
existing file so a re-run never silently double-seeds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import subprocess
import sys
import tempfile
from decimal import Decimal

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

UTC = dt.UTC


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default=str(REPO_ROOT / "picksportplus_prod_shaped.db"),
        help="Where to write the local sqlite file. Must resolve under the repo's working "
        "directory or the OS temp directory.",
    )
    parser.add_argument(
        "--reset", action="store_true", help="Delete the file first and rebuild from scratch."
    )
    return parser.parse_args()


def _assert_local_path(path: pathlib.Path) -> None:
    """Mirrors app/main.py's own local-only boot guard: this script must never be pointed at
    anything but a local file inside the repo's working directory or the OS temp directory,
    since it is test-data tooling, not a production migration tool."""
    resolved = path.resolve()
    cwd = REPO_ROOT.resolve()
    tmp = pathlib.Path(tempfile.gettempdir()).resolve()
    inside_cwd = resolved == cwd or cwd in resolved.parents
    inside_tmp = resolved == tmp or tmp in resolved.parents
    if not (inside_cwd or inside_tmp):
        raise SystemExit(
            f"Refusing to seed {resolved}: it is outside the repo's working directory and "
            "the OS temp directory. This script only ever writes a local sqlite file."
        )


def _run_migrations(db_path: pathlib.Path) -> None:
    """Runs the real `alembic upgrade head` against db_path, in a subprocess with
    DATABASE_URL overridden, so this script exercises the exact same migration path a real
    deploy does (Phase 12's own row-count safety check needs that), never a bare
    Base.metadata.create_all shortcut."""
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit("alembic upgrade head failed, see output above.")
    print("alembic upgrade head: ok")


def _add_orphaned_picks_player(db, pool) -> None:
    """A player with 16 pick rows for a 15-required pool, confidence 1..16 instead of a clean
    1..15 permutation (PICKS-REPAIR-REPORT.md's own real, observed shape): a 16th row at
    confidence 16, never a duplicate of an existing value, since uq_pick_user_week_confidence
    (the database constraint the orphaned-picks incident itself added) blocks a real
    duplicate outright. Added directly against an already-scored week, after seeding, so it
    corrupts only the stored Pick rows, never the WeekEntry score computed before this ran,
    exactly how the real incident was found (a scored week whose picks no longer add up)."""
    from sqlalchemy import select

    from app.models import Game, Pick, Week

    week = db.scalar(
        select(Week).where(Week.pool_id == pool.id, Week.week_number == 5, Week.status == "scored")
    )
    if week is None:
        print("orphaned-picks: no scored week 5 found, skipping")
        return
    player_id = db.scalar(
        select(Pick.user_id).where(Pick.week_id == week.id).order_by(Pick.user_id).limit(1)
    )
    if player_id is None:
        print("orphaned-picks: no picks found on week 5, skipping")
        return
    games = list(db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True))))
    existing_game_ids = {
        p.game_id
        for p in db.scalars(select(Pick).where(Pick.week_id == week.id, Pick.user_id == player_id))
    }
    spare = next((g for g in games if g.id not in existing_game_ids), None)
    if spare is None:
        print("orphaned-picks: no spare game to attach the 16th pick to, skipping")
        return
    now = dt.datetime.now(UTC)
    db.add(
        Pick(
            user_id=player_id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=spare.id,
            picked_team="home",
            confidence=16,
            created_at=now,
            updated_at=now,
        )
    )
    print(f"orphaned-picks: added a 16th pick row (confidence 16) for user {player_id} on week 5")


def _add_phantom_no_show_entries(db, pool) -> None:
    """The 120 point bug's own shape: WeekEntry rows flagged did_not_submit with the maximum
    inverse penalty, on a week whose lock_at is still in the future. Added directly, bypassing
    score_week_for_pool entirely, since this build's own fix (Phase 1) means the real scoring
    path can no longer produce this: this is what a stale row from BEFORE that fix deployed
    still looks like, and app/services/standings.py's read-time correction is what this
    database exists to exercise."""
    from sqlalchemy import select

    from app.models import PoolMember, Week, WeekEntry

    week = db.scalar(
        select(Week).where(Week.pool_id == pool.id, Week.week_number == 7, Week.status == "open")
    )
    if week is None:
        print("phantom-no-show: no open week 7 found, skipping")
        return
    lock_at = week.lock_at
    if lock_at is not None and lock_at.tzinfo is None:
        lock_at = lock_at.replace(tzinfo=UTC)
    if lock_at is None or lock_at <= dt.datetime.now(UTC):
        week.lock_at = dt.datetime.now(UTC) + dt.timedelta(days=3)
    member_ids = list(
        db.scalars(
            select(PoolMember.user_id)
            .where(PoolMember.pool_id == pool.id)
            .order_by(PoolMember.user_id)
        )
    )
    if len(member_ids) < 2:
        print("phantom-no-show: not enough members, skipping")
        return
    for user_id in member_ids[:2]:
        existing = db.scalar(
            select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id == user_id)
        )
        if existing is not None:
            continue
        db.add(
            WeekEntry(
                pool_id=pool.id,
                week_id=week.id,
                user_id=user_id,
                points=120,
                correct=0,
                possible=0,
                did_not_submit=True,
            )
        )
    print(f"phantom-no-show: added stale 120 point entries on week 7 (lock_at {week.lock_at})")


def _add_stale_payout_award(db, pool) -> None:
    """Simulates an award frozen before this session's tie rule shipped: bumps one real,
    already-frozen week 5 award's amount away from what a live recomputation under the new
    rule would give, so the "Awarded under the previous tie rule" label
    (app/routers/leaderboard.py, Phase 2 item 5) has something real to detect and show."""
    from sqlalchemy import select

    from app.models import PayoutAward, Week

    week = db.scalar(select(Week).where(Week.pool_id == pool.id, Week.week_number == 5))
    if week is None:
        print("stale-award: no week 5 found, skipping")
        return
    award = db.scalar(
        select(PayoutAward)
        .where(PayoutAward.pool_id == pool.id, PayoutAward.week_id == week.id)
        .order_by(PayoutAward.id)
        .limit(1)
    )
    if award is None:
        print("stale-award: no week 5 payout award found, skipping")
        return
    award.amount = award.amount + Decimal("7.00")
    print(
        f"stale-award: bumped award {award.id} to {award.amount} (now stale vs. a live recompute)"
    )


def _add_a_tied_week(db, pool) -> None:
    """Two players sharing identical points and identical weekly wins entering week 6: a
    genuine full tie under the new wins-then-split rule, so the pot for that place actually
    has to split. Applied by overwriting two already-scored WeekEntry rows directly, a
    deliberate, local-only data shape for testing, never something this build would do to a
    real week."""
    from sqlalchemy import select

    from app.models import Week, WeekEntry
    from app.services.standings import wins_entering_week

    week = db.scalar(
        select(Week).where(Week.pool_id == pool.id, Week.week_number == 6, Week.status == "scored")
    )
    if week is None:
        print("tied-week: no scored week 6 found, skipping")
        return
    entries = list(
        db.scalars(
            select(WeekEntry).where(WeekEntry.week_id == week.id).order_by(WeekEntry.user_id)
        )
    )
    if len(entries) < 2:
        print("tied-week: not enough entries, skipping")
        return
    # Picked deliberately equal on both levels (points AND prior wins entering this week), so
    # this is a genuine full tie under the wins-then-split rule, not one the wins level would
    # go on to break: two players who share the same prior win count (0, ordinarily, since
    # only one player can have won any single earlier week).
    wins_by_user = wins_entering_week(db, pool, week)
    same_wins = {}
    for entry in entries:
        same_wins.setdefault(wins_by_user.get(entry.user_id, 0), []).append(entry)
    a, b = max(same_wins.values(), key=len)[:2]
    a.points = b.points = 30
    a.correct = b.correct = 8
    a.is_winner = b.is_winner = False
    print(f"tied-week: forced users {a.user_id} and {b.user_id} to a genuine full tie on week 6")


def _add_test_week(db, pool) -> None:
    """A minimal test week (Week.is_test_week=True, week_number=0), quarantined from season
    totals, payouts and the scenarios panel by every read path that already filters on this
    flag (app/services/standings.py._season_base_rows, wins_entering_week, and friends)."""
    from app.models import Game, Week

    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=0,
        label="Test week",
        status="scored",
        is_test_week=True,
        lock_at=dt.datetime.now(UTC) - dt.timedelta(days=1),
    )
    db.add(week)
    db.flush()
    game = Game(
        week_id=week.id,
        league="nfl",
        espn_event_id="prod-shaped-test-week-game",
        start_time=dt.datetime.now(UTC) - dt.timedelta(days=1, hours=3),
        home_team="Preseason Home",
        away_team="Preseason Away",
        home_abbr="PSH",
        away_abbr="PSA",
        canonical_home_key="nfl:preseason-home",
        canonical_away_key="nfl:preseason-away",
        spread_home=-3.0,
        closeness=3.0,
        spread_source="espn",
        in_slate=True,
        slate_rank=1,
        status="final",
        winner="home",
        home_score=20,
        away_score=14,
    )
    db.add(game)
    print("test-week: added week 0 (is_test_week=True), quarantined from the real season")


def _flip_one_member_unpaid(db, pool) -> None:
    """seed_demo_pool marks every member paid by default; this flips exactly one back to
    unpaid so the payment gate and the paid/unpaid member column both have a real mix to
    show, matching a real, mid-season pool."""
    from sqlalchemy import select

    from app.models import PoolMember

    member = db.scalar(
        select(PoolMember)
        .where(PoolMember.pool_id == pool.id)
        .order_by(PoolMember.id.desc())
        .limit(1)
    )
    if member is None:
        print("unpaid-member: no members found, skipping")
        return
    member.paid_at = None
    print(f"unpaid-member: member {member.id} (user {member.user_id}) marked unpaid")


def main() -> None:
    args = _parse_args()
    db_path = pathlib.Path(args.db_path)
    _assert_local_path(db_path)

    if db_path.exists():
        if not args.reset:
            raise SystemExit(
                f"{db_path} already exists. Pass --reset to delete it and rebuild, so a "
                "plain re-run never silently double-seeds."
            )
        db_path.unlink()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()

    _run_migrations(db_path)

    # DATABASE_URL is resolved for real before any app module is imported: app.db.engine is
    # built once at import time from app.config.settings, exactly like a real boot.
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ.setdefault("OFFLINE_MODE", "true")

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db import engine
    from app.models import Pool
    from app.services.demo import seed_demo_pool

    with Session(engine, future=True) as db:
        report = seed_demo_pool(db, reset=True)
        db.commit()
        for line in report:
            print(line)

        pool = db.scalar(select(Pool).where(Pool.join_code == "DEMO2025"))
        if pool is None:
            raise SystemExit("seed_demo_pool did not create the demo pool, cannot continue.")

        _add_orphaned_picks_player(db, pool)
        _add_phantom_no_show_entries(db, pool)
        _add_stale_payout_award(db, pool)
        _add_a_tied_week(db, pool)
        _add_test_week(db, pool)
        _flip_one_member_unpaid(db, pool)
        db.commit()

    print("")
    print(f"Production-shaped database ready at {db_path}")
    print("Messy states seeded: orphaned picks, phantom no-show entries, a stale payout")
    print("award, a tied week, a voided game (from the base demo), a test week, and a")
    print("mixed paid/unpaid membership.")


if __name__ == "__main__":
    main()
