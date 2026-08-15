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
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Game, Pick, Pool, PoolMember, User, Week, utcnow
from app.providers import cfbd, espn, odds_api
from app.providers.http import BudgetExceeded, ProviderError, get_platform_settings
from app.providers.teams import match_by_teams_and_date
from app.services import calendar as calendar_svc
from app.services import mail
from app.slate import Candidate, compute_lock_at, select_slate_by_targets
from app.templating import fmt_kickoff_long

log = logging.getLogger("picksportplus.ingest")

# ESPN core odds are unmetered but cost one request per game, so a build stays polite.
MAX_CORE_ODDS_LOOKUPS = 60


# Idempotency guard (Phase 6 remediation, see DECISIONS.md) ------------------
#
# A plain, synchronous form POST with no loading feedback of any kind reasonably gets
# double-clicked, or opened in a second tab, or retried by a client whose JS never loaded.
# This app runs as a single uvicorn process, no --workers flag (see render.yaml's
# startCommand), so a simple in-process, lock-guarded set keyed by (pool_id, week_number) is a
# real, sufficient guard against two concurrent builds for the same pool week: it would NOT be
# sufficient if this service were ever run with multiple worker processes or across multiple
# instances, since each process gets its own, unshared copy of this set. That limitation is
# accepted and documented rather than built around, per DECISIONS.md.


class BuildInProgress(RuntimeError):
    """Another build for this exact (pool, week) is already running."""


_builds_lock = threading.Lock()
_builds_in_progress: set[tuple[int, int]] = set()


def _acquire_build_lock(pool_id: int, week_number: int) -> None:
    key = (pool_id, week_number)
    with _builds_lock:
        if key in _builds_in_progress:
            raise BuildInProgress("This week is already being built. Wait for it to finish.")
        _builds_in_progress.add(key)


def _release_build_lock(pool_id: int, week_number: int) -> None:
    with _builds_lock:
        _builds_in_progress.discard((pool_id, week_number))


@contextmanager
def slate_build_guard(pool_id: int, week_number: int):
    """Refuse a second concurrent build for the same (pool, week), raising BuildInProgress
    rather than queuing it. See the module note above for why an in-process set is enough here.
    Exposed as its own context manager, not folded silently into build_slate's body, so a test
    can acquire and release it directly to simulate a race deterministically (see
    tests/test_ingest.py) without needing real threads."""
    _acquire_build_lock(pool_id, week_number)
    try:
        yield
    finally:
        _release_build_lock(pool_id, week_number)


# Hard timeout (Phase 6 remediation, see DECISIONS.md) ------------------------
#
# build_slate can make several sequential HTTP calls (ESPN for the schedule, then ESPN core
# odds per game, then up to one Odds API call per league, then one CFBD call), each already
# bounded by its own settings.http_timeout_seconds/http_retries, but with no budget across the
# whole call. _Deadline is checked between major steps, not via a signal-based hard interrupt,
# the same periodic wall-clock check app/scenarios.py's own time budget already uses (see that
# module's docstring), so the message it raises can name exactly which step was in flight.


class BuildTimeout(RuntimeError):
    """A build exceeded its wall-clock budget. The message names what was in flight."""


@dataclass
class _Deadline:
    at: float
    budget_seconds: float

    def check(self, what: str) -> None:
        if time.monotonic() >= self.at:
            raise BuildTimeout(
                f"Build timed out after {int(self.budget_seconds)} seconds while waiting "
                f"for a response from {what}."
            )


def _make_deadline(time_budget_seconds: float | None) -> _Deadline | None:
    budget = (
        settings.slate_build_timeout_seconds
        if time_budget_seconds is None
        else (time_budget_seconds)
    )
    if not budget or budget <= 0:
        return None
    return _Deadline(at=time.monotonic() + budget, budget_seconds=budget)


# Schedule fetch labels for a timeout message, e.g. "ESPN for the college schedule". Deliberately
# lowercase "college" to match SPEC.md Section 3h's sentence-case, mid-sentence voice; NFL stays
# an uppercase acronym either way.
_SCHEDULE_LABELS = {"nfl": "NFL", "ncaaf": "college"}


@dataclass
class IngestReport:
    week_number: int
    season_year: int
    candidates: int = 0
    with_spread: int = 0
    selected: int = 0
    # Of the games actually selected onto the slate, how many still have no spread. Computed
    # in build_slate right after apply_slate (Phase 6 remediation), separate from with_spread
    # above, which counts across every candidate, not just the ones that made the slate. Used
    # by the commissioner facing "Week N built" flash in app/routers/admin.py, not by
    # summary() below, which predates this field and is left exactly as it was for its own
    # existing callers (the CLI, app/services/demo.py). See DECISIONS.md, Phase 6.
    missing_spread: int = 0
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
    is_test_week: bool = False,
) -> Week:
    """Get or create the pool's week row.

    week_number is always the pool's own 1, 2, 3... sequence, never an ESPN week number.
    week_number 0 is reserved for a test week (TEST_WEEK_NUMBER below), so it never collides
    with a real season week.

    anchor_date is the calendar Saturday each enabled league resolves its own ESPN week
    against, see app/services/calendar.py. Left unset, it is computed from
    pool.week1_anchor_date + (week_number - 1) weeks, when the pool has one configured. A
    pool with no week1_anchor_date gets a week with no anchor_date at all, and
    fetch_candidates falls back to sending week_number to ESPN directly (the pre anchor
    behaviour), with a clear warning that the pool needs configuring.

    is_test_week (Phase 3) only matters the first time this week row is created: it sets
    Week.is_test_week and labels the row "Test week" instead of "Week {N}". It is never
    applied to an existing row on a later call, the same one-time-on-creation shape
    upsert_games already uses for a rivalry auto-pin.
    """
    # Computed once, before branching, so a week that already exists but was created before
    # the pool had an anchor date (or before Phase 2's backfill ran) still gets backfilled
    # below. The earlier version of this function only computed anchor_date inside the "week
    # is None" branch, so an existing week's null anchor_date was never repaired even after
    # the pool itself gained one (Phase 2 remediation, see DECISIONS.md).
    if anchor_date is None and pool.week1_anchor_date is not None:
        anchor_date = pool.week1_anchor_date + dt.timedelta(weeks=week_number - 1)

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
            anchor_date=anchor_date,
            label="Test week" if is_test_week else f"Week {week_number}",
            status="draft",
            is_test_week=is_test_week,
        )
        db.add(week)
        db.flush()
    elif anchor_date is not None and week.anchor_date is None:
        # Backfill an anchor onto a week that was created before one was available, so a
        # rebuild can resolve per league dates instead of repeating the old fallback.
        week.anchor_date = anchor_date
        db.flush()
    return week


# Reserved pool week number for the commissioner's test week (Phase 3, preseason and test
# week support). Real pool weeks are always 1, 2, 3..., so 0 can never collide with one,
# which is what lets "Create a test week" stay idempotent (a second click rebuilds the same
# week rather than creating a duplicate) without any extra bookkeeping.
TEST_WEEK_NUMBER = 0


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
    db: Session, pool: Pool, week: Week, *, deadline: _Deadline | None = None
) -> tuple[list[espn.EspnGame], list[LeagueAttempt]]:
    """Every game for this pool week across its enabled leagues. ESPN only, unmetered.

    deadline (Phase 6 remediation, see DECISIONS.md), when given, is checked once per league
    right before that league's ESPN scoreboard call, so a build that has already blown its
    wall-clock budget fails fast with a message naming which league's schedule call was about
    to run, rather than piling on every remaining league's call first.

    Each league resolves its own ESPN week number and season type from week.anchor_date via
    app/services/calendar.py, because NFL and college week numbers are not aligned: college
    starts about three weeks earlier than the NFL and has a bowl season the NFL has no
    equivalent of on the same calendar. The resolution is recorded on week.resolved_weeks and
    week.is_bowl_week so the commissioner can see exactly what was asked for.

    week.is_test_week (Phase 3) is read straight off the week row, not taken as a separate
    parameter here: it is already set the moment ensure_week creates the row, so reading it
    keeps this function's own signature, and therefore every existing caller, unchanged. It
    is passed through to calendar_svc.resolve_league_week as is_test_week, which is what lets
    a test week additionally resolve against NFL preseason.

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
            if deadline is not None:
                deadline.check(f"ESPN for the {_SCHEDULE_LABELS.get(league, league)} schedule")
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
            db, league, week.season_year, week.anchor_date, is_test_week=week.is_test_week
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

        if deadline is not None:
            deadline.check(f"ESPN for the {_SCHEDULE_LABELS.get(league, league)} schedule")

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
    deadline: _Deadline | None = None,
) -> tuple[dict[str, tuple[float, str]], list[str]]:
    """Resolve a home relative spread per event id, following the Section 5e order.

    Returns (by_event_id -> (spread_home, source), warnings).

    deadline (Phase 6 remediation, see DECISIONS.md), when given, is checked once before each
    of the three provider stages below (ESPN core odds, The Odds API, CollegeFootballData),
    never inside the per-game core odds loop itself: that keeps the check at "major step"
    granularity, matching app/scenarios.py's own periodic wall-clock check, and lets the raised
    BuildTimeout name exactly which provider was about to be called.
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
        if deadline is not None:
            deadline.check("ESPN for core odds")
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
        if deadline is not None:
            deadline.check("The Odds API")
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
        if deadline is not None:
            deadline.check("CollegeFootballData")
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
            f"{len(still_missing)} games have no line posted yet. They can still be picked "
            "for the slate, ranked after every game with a known spread. Review them below "
            "and set a line by hand if you want one ranked higher."
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

        # Moneylines (Phase 8): only ever set when the feed actually carries one, never
        # cleared back to null, exactly like the spread handling above and for the same
        # reason (Spec 5a: ESPN drops odds once a game goes final, so a later sync of an
        # already-final game would otherwise wipe a value a prior, still-live sync had
        # captured). In practice these are commonly None end to end today: see
        # espn.moneyline_from_items's docstring.
        if game.home_moneyline is not None:
            row.home_moneyline = game.home_moneyline
        if game.away_moneyline is not None:
            row.away_moneyline = game.away_moneyline

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
            home_key=row.canonical_home_key,
            away_key=row.canonical_away_key,
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


def duplicate_team_warnings(db: Session, week: Week) -> list[str]:
    """Explain, in real team names, any game app.slate refused to select because one of its
    teams already plays in a game that did make the slate (Phase 2 remediation, see
    DECISIONS.md). apply_slate's own call into select_slate_by_targets already guarantees
    this never happens in the chosen slate itself; this only explains it after the fact, using
    data the pure slate module deliberately does not have (real team names), so it can be
    surfaced to the commissioner as a build warning rather than a silent drop.
    """
    rows = list(db.scalars(select(Game).where(Game.week_id == week.id)))
    on_slate_by_team: dict[str, Game] = {}
    for row in rows:
        if not row.in_slate:
            continue
        on_slate_by_team[row.canonical_home_key] = row
        on_slate_by_team[row.canonical_away_key] = row

    warnings: list[str] = []
    reported_pairs: set[frozenset[str]] = set()
    for row in rows:
        if row.in_slate or row.status == "void":
            continue
        for key, name in (
            (row.canonical_home_key, row.home_team),
            (row.canonical_away_key, row.away_team),
        ):
            other = on_slate_by_team.get(key)
            if other is None:
                continue
            pair = frozenset((row.espn_event_id, other.espn_event_id))
            if pair in reported_pairs:
                continue
            reported_pairs.add(pair)
            warnings.append(
                f"{row.away_abbr} at {row.home_abbr} was left off the slate: {name} already "
                f"plays in {other.away_abbr} at {other.home_abbr} this week. This usually "
                "means two different calendar weeks got merged. Check the week 1 anchor date "
                "in Settings."
            )
            break
    return warnings


# A rebuilt slate spanning more than this many days is a strong signal that two different
# calendar weeks got merged into one pool week (Phase 2 remediation, see DECISIONS.md): a
# normal week's games all kick off within one long weekend.
MAX_SLATE_SPAN_DAYS = 8


class SlateSpanTooWide(RuntimeError):
    """The slate's earliest and latest kickoff are too far apart to be one real pool week."""


def slate_span(db: Session, week: Week) -> tuple[int, dt.datetime, dt.datetime] | None:
    """(span_days, earliest kickoff, latest kickoff) among the week's live slate games, or
    None when fewer than two games are on the slate to compare."""
    kickoffs = sorted(
        _aware(g.start_time)
        for g in db.scalars(
            select(Game).where(
                Game.week_id == week.id, Game.in_slate.is_(True), Game.status != "void"
            )
        )
    )
    if len(kickoffs) < 2:
        return None
    earliest, latest = kickoffs[0], kickoffs[-1]
    return (latest - earliest).days, earliest, latest


def _span_too_wide_message(
    week: Week, span_days: int, earliest: dt.datetime, latest: dt.datetime
) -> str:
    parts = [
        f"This slate spans {span_days} days, from {earliest.isoformat()} to "
        f"{latest.isoformat()}, more than the {MAX_SLATE_SPAN_DAYS} day limit for one pool "
        "week. Publishing was refused."
    ]
    for league, resolution in (week.resolved_weeks or {}).items():
        label = LEAGUE_LABELS.get(league, league)
        if resolution:
            parts.append(
                f"{label} resolved to week {resolution.get('week')} "
                f"(season type {resolution.get('season_type')})."
            )
        else:
            parts.append(f"{label} did not resolve to a week for this anchor date.")
    parts.append("Check the week 1 anchor date in Settings, then rebuild the slate.")
    return " ".join(parts)


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
    with the actual spread and source for a game that made the slate by closeness. A game
    can make the slate with no line posted yet (a resolvable spread ranks a game, it never
    gates eligibility), which reads as "Closest available (no line posted yet)" rather than a
    blank or broken closeness number. No separate pin_reason column (see app/models.py,
    Game.pinned): this stays correct even if the commissioner edits the rivalry list after a
    game was pinned.
    """
    if game.pinned:
        if _rivalry_match(game.canonical_home_key, game.canonical_away_key, pool.rivalries):
            return "Rivalry"
        return "Pinned"
    if game.closeness is None:
        return "Closest available (no line posted yet)"
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
    is_test_week: bool = False,
    time_budget_seconds: float | None = None,
) -> IngestReport:
    """Build or rebuild one week. Idempotent and safe to re-run.

    Thin wrapper (Phase 6 remediation, see DECISIONS.md) around _build_slate_impl below: this
    level owns only the two things a caller cannot opt out of, the concurrent-build guard
    (slate_build_guard, raises BuildInProgress rather than letting a second build for the same
    pool week run alongside the first) and a wall-clock duration log line, so every real code
    path (a normal build, the picks-already-exist early return, the dead-end early return) is
    timed and guarded identically without duplicating that logic at each return point inside
    the implementation. See _build_slate_impl's own docstring for what the parameters mean.
    """
    started = time.monotonic()
    with slate_build_guard(pool.id, week_number):
        report = _build_slate_impl(
            db,
            pool,
            year,
            week_number,
            allow_metered=allow_metered,
            publish=publish,
            now=now,
            is_test_week=is_test_week,
            time_budget_seconds=time_budget_seconds,
        )
    elapsed = time.monotonic() - started
    log.info(
        "slate build finished, pool %s week %s, %.2fs elapsed, %s selected",
        pool.id,
        week_number,
        elapsed,
        report.selected,
    )
    return report


def _build_slate_impl(
    db: Session,
    pool: Pool,
    year: int,
    week_number: int,
    *,
    allow_metered: bool = True,
    publish: bool | None = None,
    now: dt.datetime | None = None,
    is_test_week: bool = False,
    time_budget_seconds: float | None = None,
) -> IngestReport:
    """The real build, run inside build_slate's guard and timing wrapper above.

    is_test_week (Phase 3, preseason and test week support) builds a low-stakes week from
    whatever is live right now (NFL preseason, college week 0 included) rather than the
    pool's real season, for a commissioner who wants to exercise the whole pick/score loop
    before the real season starts. It takes a different path through this function in two
    ways: it resolves against right now (see DECISIONS.md, Phase 3, for why) instead of
    requiring pool.week1_anchor_date, and it flows week.is_test_week down to fetch_candidates
    so each league's calendar resolution also tries the preseason. Everything after the week
    is created, resolving spreads, selecting the closest games, publishing, is the same
    machinery a real week goes through, unchanged.

    allow_metered (Phase 5 remediation: provider controls move to site admin) is no longer a
    commissioner's per-build choice; it is ANDed with the site admin's global, persisted
    "ESPN only" switch (app.providers.http.get_platform_settings, read fresh here on every
    call, never cached), so a commissioner cannot bypass it and a caller cannot force metered
    calls back on while the switch is on. It still defaults to True and stays a real
    parameter, both for a smaller diff (every existing caller, app/cli.py's build-slate,
    sync-week and seed-preview commands among them, keeps working unchanged) and so a trusted
    CLI operator can still pass allow_metered=False (build-slate's own --no-metered flag) to
    force one manual run ESPN only regardless of the global switch. There is no equivalent way
    to force allow_metered=True past an "on" global switch: the AND is one directional, on
    purpose, since the whole point of the switch is that nobody, commissioner or CLI operator,
    spends a credit while it is on. See DECISIONS.md, Phase 5.

    time_budget_seconds (Phase 6 remediation, see DECISIONS.md) overrides
    settings.slate_build_timeout_seconds for this one call; tests pass a small value here for a
    deterministic timeout rather than lowering the setting globally. None (the default) uses
    the configured setting; 0 or a negative number disables the budget entirely.
    """
    deadline = _make_deadline(time_budget_seconds)
    report = IngestReport(week_number=week_number, season_year=year)
    now = now or dt.datetime.now(dt.UTC)

    if is_test_week:
        # Resolves against right now, not pool.week1_anchor_date (which may be unset, or may
        # point at a Saturday weeks away): the whole point of a test week is building
        # something live before the real season is configured. See DECISIONS.md, Phase 3.
        week = ensure_week(db, pool, year, week_number, anchor_date=now.date(), is_test_week=True)
    else:
        # Refuse rather than fall back (Phase 2 remediation, see DECISIONS.md). The old
        # fallback sent the pool's own week number straight to ESPN for every league, which
        # is what produced a slate spanning two calendar weeks with the same team on it
        # twice the moment NFL and college drifted apart. week1_anchor_date is now required
        # at league creation (POST /site/leagues/new) and backfilled for any pool that
        # predates that (the backfill-anchor-dates CLI command), so hitting this in practice
        # means a commissioner cleared the field from Settings.
        if pool.week1_anchor_date is None:
            report.warnings.append(
                "Set your week 1 anchor date in Settings before building a slate. Without it "
                "the tool cannot tell which NFL and college weeks belong together."
            )
            return report

        week = ensure_week(db, pool, year, week_number)

    # Once picks exist the slate is settled. Scores still refresh, the selection does not move.
    if week_has_picks(db, week):
        report.locked_out = True
        report.warnings.append(
            "Picks have already been made for this week, so the slate was left alone. "
            "You can still void a game."
        )
        games, _attempts = fetch_candidates(db, pool, week, deadline=deadline)
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

    games, attempts = fetch_candidates(db, pool, week, deadline=deadline)
    report.candidates = len(games)
    if not games:
        report.warnings.append(_dead_end_message(db, pool, week, attempts))
        return report

    before_refreshes = week.spread_refreshes
    before_cfbd = week.cfbd_calls
    # The global switch always wins over a stale True default; it never overrides an explicit
    # allow_metered=False from a trusted caller. See this function's own docstring above.
    effective_allow_metered = allow_metered and not get_platform_settings(db).espn_only
    spreads, warnings = resolve_spreads(
        db, week, games, allow_metered=effective_allow_metered, deadline=deadline
    )
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
    # Of the games actually selected, how many still have no spread (Phase 6 remediation): the
    # "Week N built" flash names this so a commissioner does not have to open the slate editor
    # to see whether anything needs a line set by hand. Selected (app/slate.py) carries
    # closeness, not spread_home directly, but closeness_of(spread_home) is None exactly when
    # spread_home is None (see that function), so this reads the same thing without a second
    # lookup back into games/spreads by event id.
    report.missing_spread = sum(1 for c in result.selected if c.closeness is None)
    report.per_league = dict(result.per_league)
    report.shortfalls = dict(result.shortfalls)
    report.notes = list(result.notes)
    for note in result.notes:
        log.info("slate note, week %s: %s", week_number, note)
    report.warnings.extend(duplicate_team_warnings(db, week))

    should_publish = pool.auto_publish if publish is None else publish
    if should_publish and report.selected > 0 and week.status == "draft":
        try:
            report.warnings.extend(publish_week(db, week))
            report.published = True
        except SlateSpanTooWide as exc:
            # Auto publish declines rather than opens a slate that spans two calendar
            # weeks (Phase 2 remediation); the week stays a draft for the commissioner to
            # review, exactly the safety net a manual "Publish" click also gets below.
            report.warnings.append(str(exc))

    return report


def _notify_week_published(db: Session, week: Week) -> list[str]:
    """Best-effort email to every real member once a week opens for picks (Phase 7
    remediation, see DECISIONS.md), only when the pool has opted in
    (Pool.notify_week_published, off by default). Never blocks or undoes the publish itself: a
    mail failure for one member becomes one warning string here, collected the same way every
    other IngestReport.warnings entry already is, not a raised exception that would leave
    week.status flipped to "open" with no way to report what happened. actor_key is keyed by
    the recipient, not the (nonexistent) human who triggered this, since this fan-out can run
    from a live commissioner click or from the unattended sync_week cron path alike; capping
    how many notification emails any one player can receive an hour is the meaningful limit
    here, not "how many did the system send," which is not the kind of runaway abuse rate
    limiting exists to catch."""
    pool = week.pool
    if not pool.notify_week_published:
        return []
    rows = db.execute(
        select(PoolMember, User)
        .join(User, User.id == PoolMember.user_id)
        .where(PoolMember.pool_id == pool.id, User.is_active.is_(True))
    ).all()
    if not rows:
        return []

    lock_text = fmt_kickoff_long(week.lock_at, pool.timezone) if week.lock_at else "soon"
    subject = f"Week {week.week_number} is open for picks, {pool.name}"
    body = (
        "Hey,\n\n"
        f"Week {week.week_number} of {pool.name} is open for picks. Picks lock {lock_text}.\n\n"
        f"{settings.base_url}/picks"
    )
    warnings: list[str] = []
    for _member, player in rows:
        try:
            mail.send(
                db,
                to=player.email,
                subject=subject,
                html=mail.text_to_html(body),
                text=body,
                kind="week_published",
                actor_key=f"user:{player.id}",
            )
        except mail.MailError as exc:
            warnings.append(f"Could not email {player.email} about week {week.week_number}: {exc}")
    return warnings


def publish_week(db: Session, week: Week) -> list[str]:
    span = slate_span(db, week)
    if span is not None:
        span_days, earliest, latest = span
        if span_days > MAX_SLATE_SPAN_DAYS:
            raise SlateSpanTooWide(_span_too_wide_message(week, span_days, earliest, latest))
    week.status = "open"
    week.published_at = utcnow()
    db.flush()
    return _notify_week_published(db, week)


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
