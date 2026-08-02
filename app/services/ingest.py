"""Build a week: pull candidates from ESPN, resolve spreads, pick the closest N, publish.

This module owns the credit budget in practice. The rules it enforces:

  ESPN       unmetered. Every candidate list and every score refresh comes from here, and the
             core API backfills historical spreads. Called freely.
  Odds API   metered. At most settings.max_spread_refreshes_per_week live calls per week, and
             only when games are still missing a spread after ESPN has had its turn. One call
             covers a whole league.
  CFBD       metered, college only, last resort. At most settings.max_cfbd_calls_per_week live
             calls per week, and only for college games still missing a spread after the two
             steps above.

A cached response never counts against either cap, so re-running any command is free.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Game, Pick, Pool, Week, utcnow
from app.providers import cfbd, espn, odds_api
from app.providers.http import BudgetExceeded, ProviderError
from app.providers.teams import match_by_teams_and_date
from app.slate import Candidate, compute_lock_at, select_slate_by_targets

log = logging.getLogger("picksportplus.ingest")

# ESPN core odds are unmetered but cost one request per game, so a build stays polite.
MAX_CORE_ODDS_LOOKUPS = 60


@dataclass
class IngestReport:
    week_number: int
    season_year: int
    candidates: int = 0
    with_spread: int = 0
    selected: int = 0
    per_league: dict[str, int] = field(default_factory=dict)
    shortfalls: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    live_metered_calls: int = 0
    published: bool = False
    locked_out: bool = False

    def summary(self) -> str:
        parts = [
            f"Week {self.week_number}: {self.candidates} candidates, "
            f"{self.with_spread} with a spread, {self.selected} on the slate"
        ]
        if self.per_league:
            mix = ", ".join(f"{LEAGUE_LABELS.get(k, k)} {v}" for k, v in sorted(self.per_league.items()))
            parts.append(f"league mix: {mix}")
        if self.sources:
            breakdown = ", ".join(f"{k} {v}" for k, v in sorted(self.sources.items()))
            parts.append(f"spread sources: {breakdown}")
        if self.live_metered_calls:
            parts.append(f"metered calls spent: {self.live_metered_calls}")
        return ". ".join(parts) + "."


LEAGUE_LABELS = {"nfl": "NFL", "ncaaf": "College"}


# Weeks ----------------------------------------------------------------------


def ensure_week(db: Session, pool: Pool, year: int, week_number: int) -> Week:
    week = db.scalar(
        select(Week).where(
            Week.pool_id == pool.id,
            Week.season_year == year,
            Week.week_number == week_number,
        )
    )
    if week is None:
        week = Week(
            pool_id=pool.id,
            season_year=year,
            week_number=week_number,
            label=f"Week {week_number}",
            status="draft",
        )
        db.add(week)
        db.flush()
    return week


def week_has_picks(db: Session, week: Week) -> bool:
    return bool(db.scalar(select(func.count(Pick.id)).where(Pick.week_id == week.id)))


# Candidates -----------------------------------------------------------------


def fetch_candidates(db: Session, pool: Pool, year: int, week_number: int) -> list[espn.EspnGame]:
    """Every game in the week across the pool's enabled leagues. ESPN only, unmetered."""
    games: list[espn.EspnGame] = []
    for league in pool.sports or ["nfl", "ncaaf"]:
        try:
            payload = espn.fetch_scoreboard(db, league, year, week_number)
        except ProviderError as exc:
            log.warning("could not load %s week %s: %s", league, week_number, exc)
            continue
        games.extend(espn.parse_scoreboard(payload, league))
    return games


# Spread resolution ----------------------------------------------------------


def resolve_spreads(
    db: Session,
    week: Week,
    games: list[espn.EspnGame],
    *,
    allow_metered: bool = True,
    use_core_odds: bool = True,
) -> tuple[dict[str, tuple[float, str]], list[str]]:
    """Resolve a home relative spread per event id, following the Section 5e order.

    Returns (by_event_id -> (spread_home, source), warnings).
    """
    resolved: dict[str, tuple[float, str]] = {}
    warnings: list[str] = []

    # 1. ESPN scoreboard odds, already parsed onto the game. Free.
    for game in games:
        if game.spread_home is not None:
            resolved[game.event_id] = (game.spread_home, "espn")

    missing = [g for g in games if g.event_id not in resolved]

    # 2. ESPN core API. Unmetered, and the only source that still has odds for a game that
    #    has already finished, which is what makes a historical week reproducible.
    if use_core_odds and missing:
        looked_up = 0
        for game in missing:
            if looked_up >= MAX_CORE_ODDS_LOOKUPS:
                warnings.append(
                    f"Stopped ESPN core odds lookups at {MAX_CORE_ODDS_LOOKUPS} games. "
                    "Some spreads may be unresolved."
                )
                break
            try:
                payload = espn.fetch_core_odds(db, game.league, game.event_id)
            except ProviderError:
                continue
            looked_up += 1
            spread = espn.parse_core_odds(payload, game.home.abbr, game.away.abbr)
            if spread is not None:
                resolved[game.event_id] = (spread, "espn_core")
        missing = [g for g in games if g.event_id not in resolved]

    if not missing or not allow_metered:
        if missing and not allow_metered:
            warnings.append(
                f"{len(missing)} games have no ESPN spread and metered lookups were skipped."
            )
        return resolved, warnings

    # 3. The Odds API. Metered: 1 credit per league, capped per week.
    leagues_missing = sorted({g.league for g in missing})
    for league in leagues_missing:
        if week.spread_refreshes >= settings.max_spread_refreshes_per_week:
            warnings.append(
                f"Week {week.week_number} has used its "
                f"{settings.max_spread_refreshes_per_week} metered spread refreshes. "
                "Falling back to ESPN only."
            )
            break
        try:
            api_games, source = odds_api.fetch_spreads(db, league)
        except BudgetExceeded as exc:
            warnings.append(str(exc))
            continue
        except ProviderError as exc:
            warnings.append(f"The Odds API could not be read for {league}: {exc}")
            continue

        if source == "live":
            week.spread_refreshes += 1
            db.flush()

        candidates = odds_api.to_match_candidates(api_games)
        for game in [g for g in missing if g.league == league]:
            match = match_by_teams_and_date(
                game.home.canonical, game.away.canonical, game.kickoff, candidates
            )
            if match is None:
                continue
            api_game = match.payload
            if api_game.spread_home is not None:
                resolved[game.event_id] = (api_game.spread_home, "odds_api")
        missing = [g for g in games if g.event_id not in resolved]

    # 4. CFBD, college only, hard capped. The id join is exact so there is no match risk.
    college_missing = [g for g in missing if g.league == "ncaaf"]
    if college_missing and week.cfbd_calls < settings.max_cfbd_calls_per_week:
        try:
            lines, source = cfbd.fetch_lines(db, week.season_year, week.week_number)
            if source == "live":
                week.cfbd_calls += 1
                db.flush()
            by_id = cfbd.lines_by_event_id(lines)
            for game in college_missing:
                line = by_id.get(game.event_id)
                if line is not None and line.spread_home is not None:
                    resolved[game.event_id] = (line.spread_home, "cfbd")
        except BudgetExceeded as exc:
            warnings.append(str(exc))
        except ProviderError as exc:
            warnings.append(f"CollegeFootballData could not be read: {exc}")
    elif college_missing:
        warnings.append(
            f"Week {week.week_number} has used its CollegeFootballData allowance. "
            f"{len(college_missing)} college games remain without a spread."
        )

    still_missing = [g for g in games if g.event_id not in resolved]
    if still_missing:
        warnings.append(
            f"{len(still_missing)} games have no resolvable spread and were left off the "
            "slate. Review them below and set a line by hand if you want them included."
        )
    return resolved, warnings


# Persistence ----------------------------------------------------------------


def upsert_games(
    db: Session,
    week: Week,
    games: list[espn.EspnGame],
    spreads: dict[str, tuple[float, str]],
) -> list[Game]:
    """Idempotent. Matches on (week_id, espn_event_id) and updates in place."""
    existing = {
        g.espn_event_id: g for g in db.scalars(select(Game).where(Game.week_id == week.id))
    }
    rows: list[Game] = []

    for game in games:
        spread, source = spreads.get(game.event_id, (None, None))
        row = existing.get(game.event_id)
        if row is None:
            row = Game(week_id=week.id, espn_event_id=game.event_id, league=game.league)
            db.add(row)

        row.league = game.league
        row.start_time = game.kickoff
        row.home_team = game.home.name
        row.away_team = game.away.name
        row.home_abbr = game.home.abbr
        row.away_abbr = game.away.abbr
        row.home_record = game.home.record
        row.away_record = game.away.record
        row.canonical_home_key = game.home.canonical
        row.canonical_away_key = game.away.canonical

        # A commissioner's manual line is never overwritten by a feed.
        if row.spread_source != "manual":
            if spread is not None:
                row.spread_home = spread
                row.closeness = abs(spread)
                row.spread_source = source
            elif row.spread_home is None:
                row.closeness = None
                row.spread_source = None

        # A void set by the commissioner stays void.
        if row.status != "void":
            row.status = game.status
        row.home_score = game.home.score
        row.away_score = game.away.score
        if game.winner is not None:
            row.winner = game.winner

        rows.append(row)

    db.flush()
    return rows


def apply_slate(db: Session, pool: Pool, week: Week, *, now: dt.datetime | None = None):
    """Choose the closest games per league target and mark them.

    Returns the SlateResult so the caller can surface the shortfall notes.
    """
    rows = list(db.scalars(select(Game).where(Game.week_id == week.id)))
    now = now or dt.datetime.now(dt.timezone.utc)

    # Once a week is in the past, every game has kicked off, so filtering on start time
    # would empty the slate. Rebuilding a historical week is a legitimate operation.
    latest = max((_aware(g.start_time) for g in rows), default=None)
    exclude_started = bool(latest and latest > now)

    candidates = [
        Candidate(
            key=row.espn_event_id,
            league=row.league,
            kickoff=_aware(row.start_time),
            spread_home=row.spread_home,
        )
        for row in rows
        if row.status != "void"
    ]

    result = select_slate_by_targets(
        candidates,
        targets=pool.league_targets,
        total=pool.num_games_per_week,
        now=now,
        exclude_started=exclude_started,
    )
    ranks = {s.key: s.slate_rank for s in result.selected}

    for row in rows:
        rank = ranks.get(row.espn_event_id)
        row.in_slate = rank is not None
        row.slate_rank = rank

    # The session runs with autoflush off, so these must be written before recompute_lock
    # reads them back. Without this the lock query sees the previous in_slate flags and
    # lock_at comes out None, which would leave the week open forever.
    db.flush()
    recompute_lock(db, week)
    return result


def recompute_lock(db: Session, week: Week) -> None:
    """Lock at the earliest kickoff on the slate, unless the commissioner pinned a time."""
    if week.lock_at_override:
        return
    db.flush()  # autoflush is off, so read back only what is already written
    kickoffs = [
        _aware(g.start_time)
        for g in db.scalars(
            select(Game).where(
                Game.week_id == week.id, Game.in_slate.is_(True), Game.status != "void"
            )
        )
    ]
    week.lock_at = compute_lock_at(kickoffs)
    db.flush()


def reseat_ranks(db: Session, week: Week) -> None:
    """Renumber slate_rank 1..N by closeness after a commissioner edit."""
    db.flush()  # autoflush is off, so pending in_slate changes must be written first
    rows = list(
        db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True)))
    )
    rows.sort(
        key=lambda g: (
            abs(g.spread_home) if g.spread_home is not None else float("inf"),
            _aware(g.start_time),
            g.espn_event_id,
        )
    )
    for index, row in enumerate(rows, start=1):
        row.slate_rank = index
    db.flush()


# Commissioner slate editing -------------------------------------------------


class SlateLocked(RuntimeError):
    """The slate size is fixed because picks already exist."""


def can_resize_slate(db: Session, week: Week) -> bool:
    """Size and membership are editable only while no player has submitted a pick."""
    return not week_has_picks(db, week)


def add_to_slate(db: Session, week: Week, game_id: int) -> Game:
    game = _game_in_week(db, week, game_id)
    if not can_resize_slate(db, week):
        raise SlateLocked(
            "Picks already exist for this week, so the game count is fixed. "
            "You can still void a game."
        )
    if game.spread_home is None:
        raise ValueError(
            "That game has no resolved spread. Set a line by hand before adding it."
        )
    game.in_slate = True
    reseat_ranks(db, week)
    recompute_lock(db, week)
    return game


def remove_from_slate(db: Session, week: Week, game_id: int) -> Game:
    game = _game_in_week(db, week, game_id)
    if not can_resize_slate(db, week):
        raise SlateLocked(
            "Picks already exist for this week, so the game count is fixed. "
            "Void the game instead."
        )
    game.in_slate = False
    game.slate_rank = None
    reseat_ranks(db, week)
    recompute_lock(db, week)
    return game


def swap_slate_game(db: Session, week: Week, out_game_id: int, in_game_id: int) -> tuple[Game, Game]:
    """Take one game off the slate and put another on, keeping the count the same."""
    if not can_resize_slate(db, week):
        raise SlateLocked(
            "Picks already exist for this week, so the slate cannot be changed. "
            "You can still void a game."
        )
    out_game = _game_in_week(db, week, out_game_id)
    in_game = _game_in_week(db, week, in_game_id)
    if not out_game.in_slate:
        raise ValueError("That game is not on the slate.")
    if in_game.in_slate:
        raise ValueError("That game is already on the slate.")
    if in_game.spread_home is None:
        raise ValueError("That game has no resolved spread. Set a line by hand first.")
    out_game.in_slate = False
    out_game.slate_rank = None
    in_game.in_slate = True
    reseat_ranks(db, week)
    recompute_lock(db, week)
    return out_game, in_game


def set_void(db: Session, week: Week, game_id: int, void: bool) -> Game:
    """Voiding is always allowed, including after picks exist."""
    game = _game_in_week(db, week, game_id)
    if void:
        game.status = "void"
        game.winner = None
    else:
        # Back to whatever the score implies. A later fetch-results run corrects it anyway.
        if game.home_score is not None and game.away_score is not None:
            game.status = "final"
            if game.home_score > game.away_score:
                game.winner = "home"
            elif game.away_score > game.home_score:
                game.winner = "away"
            else:
                game.winner = "tie"
        else:
            game.status = "scheduled"
            game.winner = None
    recompute_lock(db, week)
    db.flush()
    return game


def set_manual_spread(db: Session, week: Week, game_id: int, spread_home: float | None) -> Game:
    """A commissioner line. Marked as manual so no feed overwrites it."""
    game = _game_in_week(db, week, game_id)
    if spread_home is None:
        game.spread_home = None
        game.closeness = None
        game.spread_source = None
    else:
        game.spread_home = float(spread_home)
        game.closeness = abs(float(spread_home))
        game.spread_source = "manual"
    db.flush()
    return game


def _game_in_week(db: Session, week: Week, game_id: int) -> Game:
    game = db.get(Game, game_id)
    if game is None or game.week_id != week.id:
        raise ValueError("That game is not part of this week.")
    return game


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


# The build ------------------------------------------------------------------


def build_slate(
    db: Session,
    pool: Pool,
    year: int,
    week_number: int,
    *,
    allow_metered: bool = True,
    publish: bool | None = None,
    now: dt.datetime | None = None,
) -> IngestReport:
    """Build or rebuild one week. Idempotent and safe to re-run."""
    report = IngestReport(week_number=week_number, season_year=year)
    week = ensure_week(db, pool, year, week_number)

    # Once picks exist the slate is settled. Scores still refresh, the selection does not move.
    if week_has_picks(db, week):
        report.locked_out = True
        report.warnings.append(
            "Picks have already been made for this week, so the slate was left alone. "
            "You can still void a game."
        )
        games = fetch_candidates(db, pool, year, week_number)
        existing_spreads = {
            g.espn_event_id: (g.spread_home, g.spread_source or "espn")
            for g in db.scalars(select(Game).where(Game.week_id == week.id))
            if g.spread_home is not None
        }
        upsert_games(db, week, games, existing_spreads)
        report.candidates = len(games)
        report.selected = int(
            db.scalar(
                select(func.count(Game.id)).where(Game.week_id == week.id, Game.in_slate.is_(True))
            )
            or 0
        )
        return report

    games = fetch_candidates(db, pool, year, week_number)
    report.candidates = len(games)
    if not games:
        report.warnings.append(
            f"ESPN returned no games for week {week_number}. Nothing to build yet."
        )
        return report

    before_refreshes = week.spread_refreshes
    before_cfbd = week.cfbd_calls
    spreads, warnings = resolve_spreads(db, week, games, allow_metered=allow_metered)
    report.warnings.extend(warnings)
    report.live_metered_calls = (week.spread_refreshes - before_refreshes) + (
        week.cfbd_calls - before_cfbd
    )
    report.with_spread = len(spreads)
    for _spread, source in spreads.values():
        report.sources[source] = report.sources.get(source, 0) + 1

    upsert_games(db, week, games, spreads)
    result = apply_slate(db, pool, week, now=now)
    report.selected = len(result.selected)
    report.per_league = dict(result.per_league)
    report.shortfalls = dict(result.shortfalls)
    report.notes = list(result.notes)
    for note in result.notes:
        log.info("slate note, week %s: %s", week_number, note)

    should_publish = pool.auto_publish if publish is None else publish
    if should_publish and report.selected > 0 and week.status == "draft":
        publish_week(db, week)
        report.published = True

    return report


def publish_week(db: Session, week: Week) -> None:
    week.status = "open"
    week.published_at = utcnow()
    db.flush()


# The set and forget entry point ---------------------------------------------


def detect_week(db: Session, pool: Pool, now: dt.datetime | None = None) -> int | None:
    """Ask ESPN which week it is. NFL drives the calendar because its weeks are canonical."""
    calendar_week = espn.detect_current_week(db, "nfl", pool.season_year, now=now)
    if calendar_week is None:
        for league in pool.sports or ["ncaaf"]:
            calendar_week = espn.detect_current_week(db, league, pool.season_year, now=now)
            if calendar_week is not None:
                break
    return calendar_week.week if calendar_week else None


def sync_week(
    db: Session, pool: Pool, now: dt.datetime | None = None, allow_metered: bool = True
) -> IngestReport | None:
    """Build and, when auto_publish is on, open the current week.

    Only builds when the week is close enough to matter, so an idle hourly cron in July
    does no work and spends nothing.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    week_number = detect_week(db, pool, now=now)
    if week_number is None:
        log.info("no current week detected for %s", pool.name)
        return None

    if pool.current_week != week_number:
        pool.current_week = week_number
        db.flush()

    report = build_slate(
        db, pool, pool.season_year, week_number, allow_metered=allow_metered, now=now
    )
    return report
