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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Game, Pick, Pool, Week, utcnow
from app.providers import cfbd, espn, odds_api
from app.providers.http import BudgetExceeded, ProviderError
from app.providers.teams import match_by_teams_and_date
from app.services import calendar as calendar_svc
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
            mix = ", ".join(
                f"{LEAGUE_LABELS.get(k, k)} {v}" for k, v in sorted(self.per_league.items())
            )
            parts.append(f"league mix: {mix}")
        if self.sources:
            breakdown = ", ".join(f"{k} {v}" for k, v in sorted(self.sources.items()))
            parts.append(f"spread sources: {breakdown}")
        if self.live_metered_calls:
            parts.append(f"metered calls spent: {self.live_metered_calls}")
        return ". ".join(parts) + "."


LEAGUE_LABELS = {"nfl": "NFL", "ncaaf": "College"}


# Weeks ----------------------------------------------------------------------


def ensure_week(
    db: Session,
    pool: Pool,
    year: int,
    week_number: int,
    *,
    anchor_date: dt.date | None = None,
) -> Week:
    """Get or create the pool's week row.

    week_number is always the pool's own 1, 2, 3... sequence, never an ESPN week number.

    anchor_date is the calendar Saturday each enabled league resolves its own ESPN week
    against, see app/services/calendar.py. Left unset, it is computed from
    pool.week1_anchor_date + (week_number - 1) weeks, when the pool has one configured. A
    pool with no week1_anchor_date gets a week with no anchor_date at all, and
    fetch_candidates falls back to sending week_number to ESPN directly (the pre anchor
    behaviour), with a clear warning that the pool needs configuring.
    """
    week = db.scalar(
        select(Week).where(
            Week.pool_id == pool.id,
            Week.season_year == year,
            Week.week_number == week_number,
        )
    )
    if week is None:
        if anchor_date is None and pool.week1_anchor_date is not None:
            anchor_date = pool.week1_anchor_date + dt.timedelta(weeks=week_number - 1)
        week = Week(
            pool_id=pool.id,
            season_year=year,
            week_number=week_number,
            anchor_date=anchor_date,
            label=f"Week {week_number}",
            status="draft",
        )
        db.add(week)
        db.flush()
    elif anchor_date is not None and week.anchor_date is None:
        # Backfill an anchor onto a week that was created before one was available, so a
        # rebuild can resolve per league dates instead of repeating the old fallback.
        week.anchor_date = anchor_date
        db.flush()
    return week


def week_has_picks(db: Session, week: Week) -> bool:
    return bool(db.scalar(select(func.count(Pick.id)).where(Pick.week_id == week.id)))


# Candidates -----------------------------------------------------------------


@dataclass
class LeagueAttempt:
    """What happened when one league was asked for its slice of a pool week.

    Kept even on success so a dead end build (every league came back with zero games) can
    explain itself precisely: what was asked for, where, and what came back.
    """

    league: str
    url: str
    resolved_week: int | None
    resolved_season_type: int | None
    games_returned: int
    error: str | None = None


def _scoreboard_url(league: str) -> str:
    segment, _extra = espn.LEAGUE_PATHS[league]
    return f"{espn.SITE_BASE}/{segment}/scoreboard"


def fetch_candidates(
    db: Session, pool: Pool, week: Week
) -> tuple[list[espn.EspnGame], list[LeagueAttempt]]:
    """Every game for this pool week across its enabled leagues. ESPN only, unmetered.

    Each league resolves its own ESPN week number and season type from week.anchor_date via
    app/services/calendar.py, because NFL and college week numbers are not aligned: college
    starts about three weeks earlier than the NFL and has a bowl season the NFL has no
    equivalent of on the same calendar. The resolution is recorded on week.resolved_weeks and
    week.is_bowl_week so the commissioner can see exactly what was asked for.

    week.anchor_date is None for a week created while the pool had no week1_anchor_date
    configured. That week falls back to the pre anchor behaviour: the pool's own week_number
    is sent to ESPN directly for every league, which is wrong whenever the two calendars have
    drifted apart, but keeps an unconfigured pool building something rather than nothing.
    """
    leagues = pool.sports or ["nfl", "ncaaf"]
    games: list[espn.EspnGame] = []
    attempts: list[LeagueAttempt] = []
    resolved: dict[str, dict[str, int] | None] = {}

    if week.anchor_date is None:
        log.warning(
            "week %s of pool %s has no anchor_date, sending week_number to ESPN directly for "
            "every league. Set pool.week1_anchor_date to resolve per league dates instead.",
            week.week_number,
            pool.id,
        )
        for league in leagues:
            resolved[league] = {"week": week.week_number, "season_type": espn.SEASON_TYPE_REGULAR}
            error: str | None = None
            league_games: list[espn.EspnGame] = []
            try:
                payload = espn.fetch_scoreboard(db, league, week.season_year, week.week_number)
                league_games = espn.parse_scoreboard(payload, league)
            except ProviderError as exc:
                error = str(exc)
                log.warning("could not load %s week %s: %s", league, week.week_number, exc)
            games.extend(league_games)
            attempts.append(
                LeagueAttempt(
                    league=league,
                    url=_scoreboard_url(league),
                    resolved_week=week.week_number,
                    resolved_season_type=espn.SEASON_TYPE_REGULAR,
                    games_returned=len(league_games),
                    error=error,
                )
            )
        week.resolved_weeks = resolved
        week.is_bowl_week = False
        return games, attempts

    any_bowl = False
    for league in leagues:
        resolution = calendar_svc.resolve_league_week(
            db, league, week.season_year, week.anchor_date
        )
        if resolution is None:
            resolved[league] = None
            attempts.append(
                LeagueAttempt(
                    league=league,
                    url=_scoreboard_url(league),
                    resolved_week=None,
                    resolved_season_type=None,
                    games_returned=0,
                    error=None,
                )
            )
            continue

        resolved[league] = {"week": resolution.week, "season_type": resolution.season_type}
        any_bowl = any_bowl or resolution.is_postseason

        error = None
        league_games = []
        try:
            payload = espn.fetch_scoreboard(
                db, league, week.season_year, resolution.week, season_type=resolution.season_type
            )
            league_games = espn.parse_scoreboard(payload, league)
        except ProviderError as exc:
            error = str(exc)
            log.warning(
                "could not load %s week %s (season_type %s): %s",
                league,
                resolution.week,
                resolution.season_type,
                exc,
            )
        games.extend(league_games)
        attempts.append(
            LeagueAttempt(
                league=league,
                url=_scoreboard_url(league),
                resolved_week=resolution.week,
                resolved_season_type=resolution.season_type,
                games_returned=len(league_games),
                error=error,
            )
        )

    week.resolved_weeks = resolved
    week.is_bowl_week = any_bowl
    return games, attempts


def _calendar_range_text(db: Session, league: str, year: int) -> str | None:
    """The valid regular season week range from that league's own calendar, for a warning."""
    try:
        payload = espn.fetch_scoreboard(
            db, league, year, ttl_minutes=calendar_svc.CALENDAR_TTL_MINUTES
        )
    except ProviderError:
        return None
    weeks = [w for w in espn.parse_calendar(payload) if w.season_type == espn.SEASON_TYPE_REGULAR]
    if not weeks:
        return None
    lo, hi = weeks[0], weeks[-1]
    return f"week {lo.week} ({lo.start.date()}) to week {hi.week} ({hi.end.date()})"


def _dead_end_message(db: Session, pool: Pool, week: Week, attempts: list[LeagueAttempt]) -> str:
    """Explain precisely why a build came back with nothing, and what the valid range was.

    Replaces the old, unhelpful "ESPN returned no games for week {N}. Nothing to build yet."
    with the anchor date, each league attempted, the resolved ESPN week or the reason none
    resolved, the URL called, the HTTP status when a request failed, the game count returned,
    and the valid week range from that league's calendar.
    """
    anchor = week.anchor_date.isoformat() if week.anchor_date else "not set"
    parts = [f"ESPN returned no games for pool week {week.week_number} (anchor date {anchor})."]

    for attempt in attempts:
        label = LEAGUE_LABELS.get(attempt.league, attempt.league)
        valid_range = _calendar_range_text(db, attempt.league, week.season_year)
        range_text = f" Valid regular season range: {valid_range}." if valid_range else ""

        if attempt.resolved_week is None:
            parts.append(
                f"{label}: the anchor date is outside both the regular season and the "
                f"postseason, so no week was resolved. Calendar checked at {attempt.url}."
                f"{range_text}"
            )
        elif attempt.error:
            parts.append(
                f"{label}: resolved to week {attempt.resolved_week} "
                f"(season type {attempt.resolved_season_type}), but the request to "
                f"{attempt.url} failed ({attempt.error}).{range_text}"
            )
        else:
            parts.append(
                f"{label}: resolved to week {attempt.resolved_week} "
                f"(season type {attempt.resolved_season_type}) at {attempt.url}, "
                f"HTTP ok, {attempt.games_returned} games returned.{range_text}"
            )

    return " ".join(parts)


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


def _rivalry_match(home_key: str, away_key: str, rivalries: list[list[str]] | None) -> bool:
    """True when the two canonical team keys match a configured rivalry pair.

    Order does not matter: a pool row [A, B] matches a game either A at B or B at A.
    A malformed pair (not exactly two entries) is ignored rather than raised, since
    this reads a JSON column that a hand edited settings save could in principle leave
    slightly odd; the fix for that is a better settings form, not an exception here.
    """
    for pair in rivalries or []:
        if len(pair) != 2:
            continue
        a, b = pair
        if {a, b} == {home_key, away_key}:
            return True
    return False


def upsert_games(
    db: Session,
    week: Week,
    games: list[espn.EspnGame],
    spreads: dict[str, tuple[float, str]],
    pool: Pool,
) -> list[Game]:
    """Idempotent. Matches on (week_id, espn_event_id) and updates in place.

    A brand new game auto-pins itself (Game.pinned = True) the moment its two teams match
    one of pool.rivalries's pairs, in either home/away order (Phase 5: "certain games with
    wider spreads are almost always included"). This only ever happens on creation, the
    first time a game is seen: a commissioner who deliberately un-pins a rivalry game while
    reviewing the slate keeps that choice on every later rebuild, since a rebuild never
    re-applies auto-pin to a game that already has a row. See DECISIONS.md, Phase 5.
    """
    existing = {g.espn_event_id: g for g in db.scalars(select(Game).where(Game.week_id == week.id))}
    rows: list[Game] = []

    for game in games:
        spread, source = spreads.get(game.event_id, (None, None))
        row = existing.get(game.event_id)
        is_new = row is None
        if is_new:
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

        if is_new:
            row.pinned = _rivalry_match(
                row.canonical_home_key, row.canonical_away_key, pool.rivalries
            )

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
    now = now or dt.datetime.now(dt.UTC)

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
            pinned=row.pinned,
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
    rows = list(db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True))))
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
        raise ValueError("That game has no resolved spread. Set a line by hand before adding it.")
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


def swap_slate_game(
    db: Session, week: Week, out_game_id: int, in_game_id: int
) -> tuple[Game, Game]:
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


def set_pinned(db: Session, week: Week, game_id: int, pinned: bool) -> Game:
    """Pin or unpin a game. Always allowed, including after picks exist.

    A pin never resizes or reorders the current slate by itself, it only changes what the
    next rebuild proposes (select_slate_by_targets guarantees a pinned candidate survives
    selection). That is why this follows set_void's "always allowed" shape rather than
    add_to_slate/remove_from_slate/swap_slate_game's can_resize_slate guard: those three
    change slate membership right now, this one only changes a future proposal.
    """
    game = _game_in_week(db, week, game_id)
    game.pinned = pinned
    db.flush()
    return game


# Human labels for spread_source, used only to explain why a game is on the slate.
SOURCE_LABELS = {
    "espn": "ESPN",
    "espn_core": "ESPN core",
    "odds_api": "The Odds API",
    "cfbd": "CollegeFootballData",
    "manual": "set by hand",
}


def slate_reason(game: Game, pool: Pool) -> str:
    """Why a game is on the slate, worked out at render time rather than stored.

    "Rivalry" when the game is pinned and its two teams match one of pool.rivalries's pairs
    in either home/away order, "Pinned" for any other commissioner set pin, and "Closest"
    with the actual spread and source for a game that made the slate on closeness alone. No
    separate pin_reason column (see app/models.py, Game.pinned): this stays correct even if
    the commissioner edits the rivalry list after a game was pinned.
    """
    if game.pinned:
        if _rivalry_match(game.canonical_home_key, game.canonical_away_key, pool.rivalries):
            return "Rivalry"
        return "Pinned"
    if game.closeness is None:
        return "Closest"
    source = SOURCE_LABELS.get(game.spread_source, "no source")
    return f"Closest (spread {game.closeness:.1f}, source {source})"


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
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


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
        games, _attempts = fetch_candidates(db, pool, week)
        existing_spreads = {
            g.espn_event_id: (g.spread_home, g.spread_source or "espn")
            for g in db.scalars(select(Game).where(Game.week_id == week.id))
            if g.spread_home is not None
        }
        upsert_games(db, week, games, existing_spreads, pool)
        report.candidates = len(games)
        report.selected = int(
            db.scalar(
                select(func.count(Game.id)).where(Game.week_id == week.id, Game.in_slate.is_(True))
            )
            or 0
        )
        return report

    games, attempts = fetch_candidates(db, pool, week)
    report.candidates = len(games)
    if not games:
        report.warnings.append(_dead_end_message(db, pool, week, attempts))
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

    upsert_games(db, week, games, spreads, pool)
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


def _local_date(now: dt.datetime, timezone: str) -> dt.date:
    """now converted to the pool's own timezone, so a late evening UTC date does not tip a
    Saturday anchor comparison into Sunday for a pool that plays on the US west coast."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = dt.UTC
    return now.astimezone(zone).date()


def _week_number_from_anchor(week1_anchor_date: dt.date, today: dt.date) -> int:
    """Which pool week's [anchor, anchor + 7 days) window today falls in, or the next
    upcoming one when today is before week 1's anchor."""
    if today < week1_anchor_date:
        return 1
    weeks_since = (today - week1_anchor_date).days // 7
    return weeks_since + 1


def detect_week(db: Session, pool: Pool, now: dt.datetime | None = None) -> int | None:
    """The pool's own current (or next upcoming) week number.

    When pool.week1_anchor_date is configured this is pure date arithmetic against the pool's
    own anchor Saturdays: no ESPN call, no dependency on any one league's calendar, and it
    naturally keeps NFL and college in step because each one resolves its own ESPN week later,
    in fetch_candidates, from the same anchor date.

    When week1_anchor_date is not configured (a pool that predates this feature, or one nobody
    has set up yet) this falls back to the pre anchor behaviour: ask ESPN what NFL's current
    week number is and use that number as the pool's own sequence. That conflates the pool's
    sequence with NFL's, which is wrong once the two calendars drift apart, but it keeps an
    unconfigured pool advancing on its own rather than building nothing. A warning is logged
    so the gap gets noticed.
    """
    now = now or dt.datetime.now(dt.UTC)

    if pool.week1_anchor_date is not None:
        today = _local_date(now, pool.timezone)
        return _week_number_from_anchor(pool.week1_anchor_date, today)

    log.warning(
        "pool %s (%s) has no week1_anchor_date configured, falling back to ESPN's NFL "
        "calendar to guess the pool's current week number. Set week1_anchor_date in the "
        "pool settings so NFL and college resolve independently.",
        pool.id,
        pool.name,
    )
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
    now = now or dt.datetime.now(dt.UTC)
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
