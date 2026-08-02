"""Command line entry points. Render Cron Jobs call these, there is no in-process scheduler.

Everything here is idempotent and safe to re-run. The one command that matters operationally
is run-cron, which is designed to be run hourly all season with no supervision.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import typer
from sqlalchemy import func, select

from app.config import settings
from app.db import engine, session_scope
from app.models import Base, Pool, PoolMember, User, Week
from app.providers.http import usage_report

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("picksportplus.cli")

app = typer.Typer(add_completion=False, help="PickSportPlus operations.")

ROOT = Path(__file__).resolve().parent.parent


def _echo(message: str) -> None:
    typer.echo(message)


def _resolve_pool(db, pool_id: int | None = None) -> Pool:
    if pool_id:
        pool = db.get(Pool, pool_id)
        if pool is None:
            raise typer.BadParameter(f"No pool with id {pool_id}.")
        return pool
    pool = db.scalars(select(Pool).order_by(Pool.id)).first()
    if pool is None:
        raise typer.Exit(code=_fail("No pool exists yet. Run: python -m app.cli seed-admin"))
    return pool


def _fail(message: str) -> int:
    typer.secho(message, fg=typer.colors.RED)
    return 1


# Schema ---------------------------------------------------------------------


@app.command("init-db")
def init_db(
    force_create_all: bool = typer.Option(
        False, "--create-all", help="Skip Alembic and create tables directly."
    ),
) -> None:
    """Create the schema, or bring it up to the latest migration."""
    alembic_ini = ROOT / "alembic.ini"
    if alembic_ini.exists() and not force_create_all:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(alembic_ini))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(cfg, "head")
        _echo("Schema is up to date (alembic upgrade head).")
        return

    Base.metadata.create_all(engine)
    _echo("Schema created.")


# Seeding --------------------------------------------------------------------


@app.command("seed-admin")
def seed_admin() -> None:
    """Create the commissioner account and the default pool from .env."""
    from app.auth import hash_password, normalize_email, normalize_join_code

    email = normalize_email(settings.admin_email)
    code = normalize_join_code(settings.default_join_code)
    if not code:
        raise typer.Exit(code=_fail("DEFAULT_JOIN_CODE is empty. Set it in .env."))

    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(settings.admin_password),
                display_name=settings.admin_display_name or "Commissioner",
                role="admin",
            )
            db.add(user)
            db.flush()
            _echo(f"Created admin user {email}.")
        else:
            if user.role != "admin":
                user.role = "admin"
            _echo(f"Admin user {email} already exists.")

        pool = db.scalar(select(Pool).where(func.upper(Pool.join_code) == code))
        if pool is None:
            pool = Pool(
                name=settings.default_pool_name,
                join_code=code,
                season_year=settings.season_year,
                num_games_per_week=settings.num_games_per_week,
                target_nfl=settings.nfl_games_per_week,
                target_ncaaf=settings.ncaaf_games_per_week,
                sports=["nfl", "ncaaf"],
                auto_publish=True,
                open_registration=settings.open_registration,
                timezone=settings.timezone,
                current_week=1,
            )
            db.add(pool)
            db.flush()
            _echo(f"Created pool {pool.name} with join code {pool.join_code}.")
        else:
            _echo(f"Pool {pool.name} already exists with join code {pool.join_code}.")

        member = db.scalar(
            select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == user.id)
        )
        if member is None:
            db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="commissioner"))
            _echo("Added the admin as commissioner of the pool.")

    _echo("")
    _echo(f"Sign in at http://localhost:8000/login as {email}")
    _echo(f"Share the join code {code} with your players.")


@app.command("seed-demo")
def seed_demo(
    reset: bool = typer.Option(False, "--reset", help="Delete and rebuild the demo pool."),
) -> None:
    """Load a real completed week with real spreads, players and picks."""
    from app.services.demo import seed_demo_pool

    with session_scope() as db:
        report = seed_demo_pool(db, reset=reset)
    for line in report:
        _echo(line)


# Weekly lifecycle -----------------------------------------------------------


@app.command("build-slate")
def build_slate_cmd(
    week: int = typer.Option(..., "--week", "-w", help="ESPN week number."),
    year: int | None = typer.Option(None, "--year", "-y"),
    pool_id: int | None = typer.Option(None, "--pool"),
    publish: bool | None = typer.Option(None, "--publish/--no-publish"),
    no_metered: bool = typer.Option(
        False, "--no-metered", help="ESPN only. Spends no Odds API or CFBD credits."
    ),
) -> None:
    """Build (or rebuild) the slate for one week."""
    from app.services.ingest import build_slate

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        report = build_slate(
            db,
            pool,
            year or pool.season_year,
            week,
            allow_metered=not no_metered,
            publish=publish,
        )
    _echo(report.summary())
    for note in report.notes:
        _echo(f"  note: {note}")
    for warning in report.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)


@app.command("publish-week")
def publish_week_cmd(
    week: int = typer.Option(..., "--week", "-w"),
    year: int | None = typer.Option(None, "--year", "-y"),
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """Open a drafted week so players can pick."""
    from app.services.ingest import publish_week

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        row = db.scalar(
            select(Week).where(
                Week.pool_id == pool.id,
                Week.season_year == (year or pool.season_year),
                Week.week_number == week,
            )
        )
        if row is None:
            raise typer.Exit(code=_fail(f"Week {week} has not been built yet."))
        if row.status != "draft":
            _echo(f"Week {week} is already {row.status}.")
            return
        publish_week(db, row)
    _echo(f"Week {week} is open.")


@app.command("sync-week")
def sync_week_cmd(
    pool_id: int | None = typer.Option(None, "--pool"),
    no_metered: bool = typer.Option(False, "--no-metered"),
) -> None:
    """Detect the current week, build it, and open it when auto publish is on."""
    from app.services.ingest import sync_week

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        report = sync_week(db, pool, allow_metered=not no_metered)
    if report is None:
        _echo("No current week detected. Nothing to do.")
        return
    _echo(report.summary())
    for note in report.notes:
        _echo(f"  note: {note}")
    for warning in report.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)


@app.command("fetch-results")
def fetch_results_cmd(
    week: int | None = typer.Option(None, "--week", "-w"),
    year: int | None = typer.Option(None, "--year", "-y"),
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """Pull finals and status from ESPN. Unmetered."""
    from app.services.results import fetch_results

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        row = _week_or_current(db, pool, week, year)
        if row is None:
            _echo("No week to refresh.")
            return
        report = fetch_results(db, pool, row)
    _echo(report.summary())
    for warning in report.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)


@app.command("score-week")
def score_week_cmd(
    week: int | None = typer.Option(None, "--week", "-w"),
    year: int | None = typer.Option(None, "--year", "-y"),
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """Compute pick results, week entries and standings."""
    from app.services.results import score_week_for_pool

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        row = _week_or_current(db, pool, week, year)
        if row is None:
            _echo("No week to score.")
            return
        report = score_week_for_pool(db, pool, row)
    _echo(report.summary())


@app.command("run-cron")
def run_cron(
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """The set and forget entry point. Safe to run hourly all season.

    ESPN work happens every run because it is free. Metered spread lookups only happen
    while a slate is actually being built or refreshed, and only inside the per week cap.
    """
    from app.services.ingest import sync_week
    from app.services.results import fetch_results, score_week_for_pool

    started = dt.datetime.now(dt.UTC)
    with session_scope() as db:
        pools = list(db.scalars(select(Pool).order_by(Pool.id)))
        if pool_id:
            pools = [p for p in pools if p.id == pool_id]
        if not pools:
            _echo("No pools configured. Run seed-admin first.")
            return

        for pool in pools:
            _echo(f"[{pool.name}]")
            report = sync_week(db, pool)
            if report is not None:
                _echo(f"  {report.summary()}")
                for note in report.notes:
                    _echo(f"  note: {note}")
                for warning in report.warnings:
                    typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)

            # Refresh and score every week that is still live. Cheap, ESPN only.
            live = list(
                db.scalars(
                    select(Week)
                    .where(
                        Week.pool_id == pool.id,
                        Week.season_year == pool.season_year,
                        Week.status.in_(("open", "locked")),
                    )
                    .order_by(Week.week_number)
                )
            )
            for row in live:
                results = fetch_results(db, pool, row)
                _echo(f"  {results.summary()}")
                score = score_week_for_pool(db, pool, row)
                _echo(f"  {score.summary()}")

    elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
    _echo(f"Done in {elapsed:.1f}s.")


@app.command("usage")
def usage_cmd() -> None:
    """Show where the metered API budgets stand this month."""
    with session_scope() as db:
        for row in usage_report(db):
            state = (
                "not configured"
                if not row["configured"]
                else ("EXHAUSTED" if row["exhausted"] else "ok")
            )
            _echo(
                f"{row['label']:<22} {row['period']}  "
                f"{row['used']:>4} / {row['budget']:<4} used ({row['pct']}%)  {state}"
            )
            if row["provider_remaining"] is not None:
                _echo(f"{'':<22} provider reports {row['provider_remaining']} remaining")
            if row["last_error"]:
                _echo(f"{'':<22} last error: {row['last_error']}")
    _echo("")
    _echo("ESPN is keyless and unmetered. Schedules and scores never spend credits.")


def _week_or_current(db, pool: Pool, week: int | None, year: int | None) -> Week | None:
    year = year or pool.season_year
    if week is not None:
        return db.scalar(
            select(Week).where(
                Week.pool_id == pool.id,
                Week.season_year == year,
                Week.week_number == week,
            )
        )
    return db.scalar(
        select(Week)
        .where(Week.pool_id == pool.id, Week.season_year == year)
        .where(Week.status.in_(("open", "locked", "scored")))
        .order_by(Week.week_number.desc())
    )


if __name__ == "__main__":
    app()
