"""Command line entry points. Render Cron Jobs call these, there is no in-process scheduler.

Everything here is idempotent and safe to re-run. The one command that matters operationally
is run-cron, which is designed to be run hourly all season with no supervision.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import typer
from sqlalchemy import func, select

from app.config import settings
from app.db import engine, session_scope
from app.models import PAYOUT_SCOPES, Base, Pool, User, Week
from app.providers import espn
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
    # is_preview excluded from the default pool lookup (Post-launch fixes, see
    # DECISIONS.md): an operator command run without an explicit --pool must never silently
    # land on the hidden preview pool. Passing --pool explicitly still works, for seed-preview
    # itself and for anyone who genuinely wants to target it by id.
    pool = db.scalars(select(Pool).where(Pool.is_preview.is_(False)).order_by(Pool.id)).first()
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
    """Create the commissioner account and the default pool from .env.

    Safe to run on every boot. It exits 0 and does nothing when ADMIN_EMAIL or
    ADMIN_PASSWORD are unset, which is the normal case for a public demo where the
    start command chains this between the migration and the server.
    """
    from app.auth import hash_password, normalize_email, normalize_join_code

    if not settings.has_admin_credentials:
        _echo(
            "ADMIN_EMAIL and ADMIN_PASSWORD are not both set, so seed-admin did nothing. "
            "Set them if you want your own commissioner account."
        )
        return

    email = normalize_email(settings.admin_email)
    code = normalize_join_code(settings.default_join_code)
    if not code or code == "MAKE-ONE-UP":
        _echo("DEFAULT_JOIN_CODE is not set, so seed-admin did nothing.")
        return

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
                # Off by default (Phase 5): a freshly seeded pool builds a draft and waits for
                # the commissioner to review and publish it, matching Pool.auto_publish's own
                # model default. Left unset here rather than hard coded True, which is what
                # used to silently keep every new pool auto publishing even after the model
                # default changed.
                open_registration=settings.open_registration,
                timezone=settings.timezone,
                current_week=1,
                # Real, confirmed anchor for the season (see DECISIONS.md, Phase 9b). Nullable,
                # so leaving WEEK1_ANCHOR_DATE unset in the environment still seeds a working
                # pool, just one that falls back to detect_week's pre-anchor behaviour until a
                # commissioner sets it from /admin/settings.
                week1_anchor_date=settings.week1_anchor_date,
            )
            db.add(pool)
            db.flush()
            _echo(f"Created pool {pool.name} with join code {pool.join_code}.")
        else:
            _echo(f"Pool {pool.name} already exists with join code {pool.join_code}.")

        # The admin is never enrolled as a PoolMember here (Post-launch fixes, see
        # DECISIONS.md): the site admin manages every league from /admin/leagues but never
        # runs one, structurally, not just by convention. The pool created above still needs
        # a real commissioner attached, either by email from /admin/leagues/new or by sharing
        # its commissioner invite link, before anyone can manage it day to day.

    _echo("")
    _echo(f"Sign in at http://localhost:8000/login as {email}")
    _echo(f"{pool.name} has no commissioner yet. Attach one from the admin portal, either by")
    _echo("email at /admin/leagues/new or with that league's commissioner invite link.")


@app.command("seed-demo")
def seed_demo(
    reset: bool = typer.Option(False, "--reset", help="Delete and rebuild the demo pool."),
    scenario_week: bool = typer.Option(
        False,
        "--scenario-week",
        help=(
            "Leave the second historical week (week 6) partially played, some games final "
            "and the rest reverted to pending, instead of fully scored, so the Scenarios "
            "panel has a real week to open against."
        ),
    ),
) -> None:
    """Load three real weeks (two scored, one open) with real spreads, players and picks."""
    from app.services.demo import seed_demo_pool

    with session_scope() as db:
        report = seed_demo_pool(db, reset=reset, scenario_week=scenario_week)
    for line in report:
        _echo(line)


@app.command("seed-preview")
def seed_preview_cmd(
    week: int | None = typer.Option(
        None, "--week", "-w", help="Pool's own week number. Defaults to the detected current week."
    ),
    no_metered: bool = typer.Option(
        False, "--no-metered", help="ESPN only. Spends no Odds API or CFBD credits."
    ),
) -> None:
    """Create the preview pool if it does not exist yet, and build (and publish) its current
    week's slate.

    The only way the preview pool's games are ever built or refreshed: never automatic, never
    triggered by a page view, only this explicit, human-triggered command. Safe to re-run;
    reuses the same is_preview pool every time and rebuilds whichever week is current.
    """
    from app.services.ingest import build_slate, detect_week
    from app.services.preview import ensure_preview_pool

    with session_scope() as db:
        pool = ensure_preview_pool(db)
        db.flush()
        week_number = week or detect_week(db, pool) or pool.current_week
        report = build_slate(
            db,
            pool,
            pool.season_year,
            week_number,
            allow_metered=not no_metered,
            publish=True,
        )
        pool_id, pool_name = pool.id, pool.name
    _echo(f"Preview pool: {pool_name} (id {pool_id})")
    _echo(report.summary())
    for note in report.notes:
        _echo(f"  note: {note}")
    for warning in report.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)


# Weekly lifecycle -----------------------------------------------------------


@app.command("build-slate")
def build_slate_cmd(
    week: int = typer.Option(
        ..., "--week", "-w", help="The pool's own week number, not an ESPN week number."
    ),
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
        # is_preview excluded (Post-launch fixes, see DECISIONS.md): run-cron is exactly the
        # kind of automatic, scheduled trigger the preview pool must never be refreshed by.
        # Its slate is only ever built by the explicit seed-preview command.
        pools = list(db.scalars(select(Pool).where(Pool.is_preview.is_(False)).order_by(Pool.id)))
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


# Payouts ----------------------------------------------------------------------


def _dollars(value: Decimal) -> str:
    return f"${value:,.2f}"


@app.command("payouts-show")
def payouts_show_cmd(
    scope: str | None = typer.Option(
        None, "--scope", "-s", help="Restrict to one payout scope. Shows all four when omitted."
    ),
    pool_id: int | None = typer.Option(None, "--pool"),
    year: int | None = typer.Option(
        None,
        "--year",
        "-y",
        help=(
            "Accepted for symmetry with the other payout commands. Payout rules and the pot "
            "are pool wide, not year scoped, so this has no effect on the output."
        ),
    ),
) -> None:
    """Print the current payout ladder and allocation summary against the effective pot.

    Read only, writes nothing. Safe to run at any time, as often as you like, matching this
    file's own guarantee that every command here is idempotent.
    """
    from app.payouts import allocation_summary
    from app.services.payouts import effective_pot, load_rules

    del year  # See the --year help text: payout rules and the pot are not year scoped.

    if scope is not None and scope not in PAYOUT_SCOPES:
        raise typer.Exit(
            code=_fail(f"Unknown scope {scope!r}. Choose one of: {', '.join(PAYOUT_SCOPES)}.")
        )

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        pool_name, pool_ref = pool.name, pool.id
        rules = load_rules(db, pool)
        pot = effective_pot(db, pool)
        weekly_weeks = pool.weekly_payout_weeks
        summary = allocation_summary(rules, pot=pot, weekly_weeks=weekly_weeks)

    scopes_to_show = [scope] if scope else list(PAYOUT_SCOPES)

    _echo(f"Payouts for {pool_name} (id {pool_ref})")
    _echo(f"Pot: {_dollars(pot)}")
    _echo("")
    for scope_name in scopes_to_show:
        scope_summary = summary.scopes[scope_name]
        _echo(scope_name)
        if not scope_summary.places:
            _echo("  (no rules configured)")
        else:
            for place in sorted(scope_summary.places):
                _echo(f"  place {place}: {_dollars(scope_summary.places[place])}")
        weeks_note = f" over {weekly_weeks} weeks" if scope_name == "weekly" else ""
        _echo(
            f"  category total{weeks_note}: {_dollars(scope_summary.category_total)} "
            f"({scope_summary.percent_of_pot:.2f}% of pot)"
        )
        _echo("")

    _echo(f"Grand total : {_dollars(summary.grand_total)}")
    _echo(f"Pot         : {_dollars(summary.pot)}")
    _echo(f"Unallocated : {_dollars(summary.unallocated)}")


@app.command("payouts-preset")
def payouts_preset_cmd(
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """Replace every payout rule for a pool with the known preset ladder.

    Idempotent and safe to re-run: load_preset (app/services/payouts.py) always clears and
    reseeds the same 12 rows first, so running this command twice in a row reseeds the
    identical ladder both times, never raising and never duplicating a row.
    """
    from app.services.payouts import load_preset

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        pool_name = pool.name
        count = load_preset(db, pool)
    _echo(f"Seeded {count} payout rules for {pool_name} from the preset ladder.")


@app.command("payouts-snapshot")
def payouts_snapshot_cmd(
    scope: str = typer.Option(..., "--scope", "-s", help="Which payout scope to snapshot."),
    week: int | None = typer.Option(None, "--week", "-w"),
    year: int | None = typer.Option(None, "--year", "-y"),
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """Snapshot one payout scope by hand right now, freezing its awards.

    --week is required for "weekly"/"bowl" and must be omitted for "season_points"/
    "season_wins", which never carry a week. Idempotent per snapshot_awards's own contract
    (app/services/payouts.py): re-running this with nothing changed underneath leaves the
    existing frozen amounts exactly as they were.
    """
    from app.services.payouts import snapshot_awards

    if scope not in PAYOUT_SCOPES:
        raise typer.Exit(
            code=_fail(f"Unknown scope {scope!r}. Choose one of: {', '.join(PAYOUT_SCOPES)}.")
        )

    is_season_scope = scope in ("season_points", "season_wins")
    if is_season_scope and week is not None:
        raise typer.Exit(code=_fail(f"--week does not apply to the {scope!r} scope. Omit it."))
    if not is_season_scope and week is None:
        raise typer.Exit(code=_fail(f"--week is required for the {scope!r} scope."))

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        pool_name = pool.name
        week_row = None
        if not is_season_scope:
            week_row = _week_or_current(db, pool, week, year)
            if week_row is None:
                raise typer.Exit(code=_fail(f"Week {week} has not been built yet."))
        awards = snapshot_awards(db, pool, scope, week=week_row)
        count = len(awards)

    if is_season_scope:
        _echo(f"Snapshotted {count} award(s) for {pool_name}, scope={scope} (season).")
    else:
        _echo(f"Snapshotted {count} award(s) for {pool_name}, scope={scope}, week={week}.")


@app.command("payouts-summary")
def payouts_summary_cmd(
    pool_id: int | None = typer.Option(None, "--pool"),
) -> None:
    """Print the commissioner payout summary: one row per pool member, every scope's total,
    the grand total, and the paid/unpaid split.

    Read only, writes nothing. Reads only frozen PayoutAward rows, the same data the
    /admin/payouts/summary page shows, safe to run at any time.
    """
    from app.services.payouts import payout_summary

    with session_scope() as db:
        pool = _resolve_pool(db, pool_id)
        pool_name = pool.name
        table = [
            (
                row.display_name,
                row.weekly_total,
                row.bowl_total,
                row.season_points_total,
                row.season_wins_total,
                row.grand_total,
                row.paid_total,
                row.unpaid_total,
            )
            for row in payout_summary(db, pool)
        ]

    _echo(f"Payout summary for {pool_name}")
    _echo("")
    columns = ("Player", "Weekly", "Bowl", "Season Pts", "Season Wins", "Total", "Paid", "Unpaid")
    header = f"{columns[0]:<24}" + "".join(f"{c:>12}" for c in columns[1:])
    _echo(header)
    _echo("-" * len(header))

    # weekly, bowl, season_points, season_wins, grand, paid totals; unpaid is derived, not
    # summed independently, so it always reconciles with the other columns by construction.
    totals = [Decimal("0")] * 6
    for name, weekly, bowl, season_points, season_wins, grand, paid, unpaid in table:
        _echo(
            f"{name:<24}"
            f"{_dollars(weekly):>12}{_dollars(bowl):>12}{_dollars(season_points):>12}"
            f"{_dollars(season_wins):>12}{_dollars(grand):>12}{_dollars(paid):>12}"
            f"{_dollars(unpaid):>12}"
        )
        for i, value in enumerate((weekly, bowl, season_points, season_wins, grand, paid)):
            totals[i] += value

    _echo("-" * len(header))
    total_unpaid = totals[4] - totals[5]
    _echo(
        f"{'Total':<24}"
        f"{_dollars(totals[0]):>12}{_dollars(totals[1]):>12}{_dollars(totals[2]):>12}"
        f"{_dollars(totals[3]):>12}{_dollars(totals[4]):>12}{_dollars(totals[5]):>12}"
        f"{_dollars(total_unpaid):>12}"
    )


def _redact_db_url(url: str) -> str:
    """Hide a password in a postgres URL. Never print a live secret."""
    if "@" not in url:
        return url
    scheme_and_creds, _, rest = url.partition("@")
    scheme, _, _creds = scheme_and_creds.partition("//")
    return f"{scheme}//***@{rest}"


@app.command("doctor")
def doctor(
    pool_id: int | None = typer.Option(None, "--pool"),
    probe: bool = typer.Option(
        True, "--probe/--no-probe", help="Also hit ESPN live for a real scoreboard."
    ),
) -> None:
    """Diagnose the setup without changing any data.

    Prints the database, migration state, OFFLINE_MODE, which provider keys are present
    (presence only, never the value), pool settings, and for each enabled league a live
    ESPN scoreboard probe showing the URL called, the HTTP status, the returned season
    year, the returned week number, the game count, and the valid calendar week range.
    """
    import httpx

    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    _echo("Database")
    _echo(f"  dialect     : {engine.dialect.name}")
    _echo(f"  url         : {_redact_db_url(settings.database_url)}")

    alembic_ini = ROOT / "alembic.ini"
    if alembic_ini.exists():
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        state = "up to date" if current == head else f"BEHIND (at {current}, head is {head})"
        _echo(f"  migrations  : {state}")
    else:
        _echo("  migrations  : no alembic.ini found")

    _echo("")
    _echo(f"OFFLINE_MODE  : {settings.offline_mode}")
    _echo("")
    _echo("Provider keys (presence only, values are never printed)")
    _echo(f"  ODDS_API_KEY : {'set' if settings.odds_api_key else 'NOT SET'}")
    _echo(f"  CFBD_API_KEY : {'set' if settings.cfbd_api_key else 'NOT SET'}")

    with session_scope() as db:
        pool = (
            db.get(Pool, pool_id)
            if pool_id
            else db.scalars(
                select(Pool).where(Pool.is_preview.is_(False)).order_by(Pool.id)
            ).first()
        )
        if pool is None:
            _echo("")
            _echo("No pool exists yet. Run: python -m app.cli seed-admin")
            return

        _echo("")
        _echo(f"Pool: {pool.name} (id {pool.id})")
        _echo(f"  season_year        : {pool.season_year}")
        _echo(f"  sports             : {pool.sports}")
        _echo(f"  num_games_per_week : {pool.num_games_per_week}")
        _echo(f"  picks_required     : {pool.picks_required}")
        _echo(f"  target_nfl/ncaaf   : {pool.target_nfl} / {pool.target_ncaaf}")
        _echo(f"  auto_publish       : {pool.auto_publish}")
        _echo(f"  current_week       : {pool.current_week}")
        _echo(f"  timezone           : {pool.timezone}")

        if not probe:
            return

        _echo("")
        _echo("Live ESPN scoreboard probe (unmetered, no data written)")
        for league in pool.sports or ["nfl", "ncaaf"]:
            segment, extra = espn.LEAGUE_PATHS[league]
            params: dict[str, object] = {"seasontype": 2, "dates": pool.season_year, **extra}
            url = f"{espn.SITE_BASE}/{segment}/scoreboard"
            query = "&".join(f"{k}={v}" for k, v in params.items())
            _echo(f"  [{league}]")
            _echo(f"    url    : {url}?{query}")
            try:
                resp = httpx.get(
                    url,
                    params=params,
                    timeout=settings.http_timeout_seconds,
                    headers={"User-Agent": "PickSportPlus/1.0 (weekly pick em pool)"},
                )
            except httpx.HTTPError as exc:
                _echo(f"    status : request failed ({exc})")
                continue
            _echo(f"    status : HTTP {resp.status_code}")
            if resp.status_code != 200:
                continue
            payload = resp.json()
            season = payload.get("season") or {}
            week = payload.get("week") or {}
            events = payload.get("events") or []
            _echo(f"    season year returned : {season.get('year')}")
            _echo(f"    week number returned : {week.get('number')}")
            _echo(f"    game count           : {len(events)}")
            calendar_weeks = [
                c for c in espn.parse_calendar(payload) if c.season_type == espn.SEASON_TYPE_REGULAR
            ]
            if calendar_weeks:
                lo, hi = calendar_weeks[0], calendar_weeks[-1]
                _echo(
                    f"    valid regular season range : week {lo.week} ({lo.start.date()}) "
                    f"to week {hi.week} ({hi.end.date()})"
                )


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
