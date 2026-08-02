"""ESPN feeds. Schedules, live status, final scores, and odds when the game is not finished.

ESPN is undocumented, so everything here is coded against payloads recorded from the live API
on 2026-08-02. The recordings live in tests/fixtures and the parsing tests run against them.

Two behaviours that are not obvious and that the rest of the app depends on:

1. The scoreboard drops competitions[0].odds once a game is completed. Odds are only present
   for upcoming and in progress games. Historical spreads come from the core API instead, see
   fetch_core_odds.
2. The raw sign of odds["spread"] is not trustworthy across payloads. The reliable derivation
   is the homeTeamOdds/awayTeamOdds "favorite" booleans combined with the magnitude, with the
   "ABBR -X.X" details string as the final fallback.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.providers.http import ProviderError, fetch_json
from app.providers.teams import canonical_key

log = logging.getLogger("picksportplus.espn")

SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/football"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues"

# league key -> (ESPN path segment, extra scoreboard query)
LEAGUE_PATHS: dict[str, tuple[str, dict[str, str]]] = {
    "nfl": ("nfl", {}),
    "ncaaf": ("college-football", {"groups": "80"}),  # groups=80 is FBS
}

SEASON_TYPE_REGULAR = 2
SEASON_TYPE_POSTSEASON = 3

_PICK_EM = {"even", "pk", "pick", "pick em", "pickem"}


@dataclass(frozen=True)
class TeamSide:
    name: str
    abbr: str
    canonical: str
    record: str | None = None
    score: int | None = None


@dataclass(frozen=True)
class EspnGame:
    event_id: str
    league: str
    kickoff: dt.datetime
    home: TeamSide
    away: TeamSide
    status: str  # scheduled | in_progress | final | void
    winner: str | None  # home | away | tie | None
    spread_home: float | None = None
    spread_source: str | None = None
    neutral_site: bool = False

    @property
    def is_final(self) -> bool:
        return self.status == "final"


@dataclass(frozen=True)
class CalendarWeek:
    season_type: int
    week: int
    label: str
    start: dt.datetime
    end: dt.datetime


# Fetching -------------------------------------------------------------------


def _path(league: str) -> tuple[str, dict[str, str]]:
    try:
        return LEAGUE_PATHS[league]
    except KeyError:
        raise ValueError(f"Unknown league {league!r}. Use nfl or ncaaf.") from None


def fetch_scoreboard(
    db: Session,
    league: str,
    year: int,
    week: int | None = None,
    season_type: int = SEASON_TYPE_REGULAR,
    ttl_minutes: int = 10,
) -> Any:
    """Scoreboard for one league and week. Unmetered, so this is safe to call often."""
    segment, extra = _path(league)
    params: dict[str, Any] = {"seasontype": season_type, "dates": year, **extra}
    if week is not None:
        params["week"] = week
    key = f"espn:scoreboard:{league}:{year}:{season_type}:{week}"
    payload, source = fetch_json(
        db,
        cache_key=key,
        url=f"{SITE_BASE}/{segment}/scoreboard",
        params=params,
        ttl_minutes=ttl_minutes,
    )
    if source != "live":
        log.info("scoreboard %s served from %s", key, source)
    return payload


def fetch_core_odds(db: Session, league: str, event_id: str) -> Any:
    """Odds from the core API, which retains them for completed games.

    This is what lets a historical demo week carry real spreads. Unmetered.
    """
    segment, _ = _path(league)
    key = f"espn:coreodds:{league}:{event_id}"
    payload, _source = fetch_json(
        db,
        cache_key=key,
        url=f"{CORE_BASE}/{segment}/events/{event_id}/competitions/{event_id}/odds",
        # Historical odds never change, so cache them effectively forever.
        ttl_minutes=60 * 24 * 30,
    )
    return payload


# Parsing --------------------------------------------------------------------


def _parse_dt(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _int_or_none(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _status_of(event: dict) -> str:
    stype = ((event.get("status") or {}).get("type")) or {}
    name = (stype.get("name") or "").upper()
    if name in ("STATUS_CANCELED", "STATUS_POSTPONED"):
        return "void"
    if stype.get("completed"):
        return "final"
    state = (stype.get("state") or "").lower()
    if state == "in":
        return "in_progress"
    if state == "post":
        # Completed is false but the game is over, for example a forfeit. Treat as void
        # rather than final, because there is no trustworthy winner to score.
        return "void"
    return "scheduled"


def _record_of(competitor: dict) -> str | None:
    for record in competitor.get("records") or []:
        if record.get("type") == "total":
            return record.get("summary")
    records = competitor.get("records") or []
    return records[0].get("summary") if records else None


def parse_spread_item(item: dict, home_abbr: str, away_abbr: str) -> float | None:
    """Derive the home relative spread from one bookmaker entry.

    Negative means the home team is favoured. Returns None when nothing is trustworthy.
    """
    details = (item.get("details") or "").strip()
    if details.lower() in _PICK_EM:
        return 0.0

    raw_spread = item.get("spread")
    magnitude: float | None = None
    if isinstance(raw_spread, int | float):
        magnitude = abs(float(raw_spread))

    home_odds = item.get("homeTeamOdds") or {}
    away_odds = item.get("awayTeamOdds") or {}

    # Preferred path: the explicit favourite flags plus the magnitude.
    if magnitude is not None:
        if magnitude == 0:
            return 0.0
        if home_odds.get("favorite") is True:
            return -magnitude
        if away_odds.get("favorite") is True:
            return magnitude

    # Fallback: parse "KC -3.5" and decide by abbreviation.
    parsed = _parse_details(details)
    if parsed is not None:
        abbr, value = parsed
        if abbr and abbr.upper() == (home_abbr or "").upper():
            return -abs(value)
        if abbr and abbr.upper() == (away_abbr or "").upper():
            return abs(value)

    return None


def _parse_details(details: str) -> tuple[str, float] | None:
    """'KC -3.5' -> ('KC', 3.5). Returns None when the string is not that shape."""
    if not details:
        return None
    parts = details.split()
    if len(parts) < 2:
        return None
    abbr = parts[0]
    try:
        value = float(parts[-1])
    except ValueError:
        return None
    return abbr, value


def spread_from_items(items: list[dict], home_abbr: str, away_abbr: str) -> float | None:
    """Median across books, so one outlier line cannot pick the slate."""
    values = [
        v
        for v in (parse_spread_item(item, home_abbr, away_abbr) for item in items or [])
        if v is not None
    ]
    if not values:
        return None
    return float(statistics.median(values))


def parse_core_odds(payload: Any, home_abbr: str, away_abbr: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    return spread_from_items(payload.get("items") or [], home_abbr, away_abbr)


def parse_event(event: dict, league: str) -> EspnGame | None:
    """One scoreboard event into an EspnGame. Returns None when it is unusable."""
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]
    competitors = comp.get("competitors") or []

    home_raw = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away_raw = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home_raw or not away_raw:
        return None

    kickoff = _parse_dt(event.get("date") or comp.get("date"))
    if kickoff is None:
        return None

    def side(competitor: dict) -> TeamSide:
        team = competitor.get("team") or {}
        name = team.get("displayName") or team.get("location") or team.get("name") or "Unknown"
        abbr = (team.get("abbreviation") or name[:4]).upper()
        return TeamSide(
            name=name,
            abbr=abbr,
            canonical=canonical_key(name, league, abbr),
            record=_record_of(competitor),
            score=_int_or_none(competitor.get("score")),
        )

    home, away = side(home_raw), side(away_raw)
    status = _status_of(event)

    winner: str | None = None
    if status == "final":
        if home.score is not None and away.score is not None:
            if home.score > away.score:
                winner = "home"
            elif away.score > home.score:
                winner = "away"
            else:
                winner = "tie"
        elif home_raw.get("winner") is True:
            winner = "home"
        elif away_raw.get("winner") is True:
            winner = "away"

    spread = spread_from_items(comp.get("odds") or [], home.abbr, away.abbr)

    return EspnGame(
        event_id=str(event.get("id")),
        league=league,
        kickoff=kickoff,
        home=home,
        away=away,
        status=status,
        winner=winner,
        spread_home=spread,
        spread_source="espn" if spread is not None else None,
        neutral_site=bool(comp.get("neutralSite")),
    )


def parse_scoreboard(payload: Any, league: str) -> list[EspnGame]:
    if not isinstance(payload, dict):
        return []
    games: list[EspnGame] = []
    for event in payload.get("events") or []:
        try:
            game = parse_event(event, league)
        except Exception as exc:  # one malformed event must not lose the week
            log.warning("skipping unparsable event: %s", exc)
            continue
        if game is not None:
            games.append(game)
    return games


# Week detection -------------------------------------------------------------


def parse_calendar(payload: Any) -> list[CalendarWeek]:
    """Flatten leagues[0].calendar into a sorted list of weeks."""
    if not isinstance(payload, dict):
        return []
    leagues = payload.get("leagues") or []
    if not leagues:
        return []
    out: list[CalendarWeek] = []
    for group in leagues[0].get("calendar") or []:
        if not isinstance(group, dict):
            continue
        try:
            season_type = int(group.get("value"))
        except (TypeError, ValueError):
            continue
        for entry in group.get("entries") or []:
            start = _parse_dt(entry.get("startDate"))
            end = _parse_dt(entry.get("endDate"))
            try:
                week = int(entry.get("value"))
            except (TypeError, ValueError):
                continue
            if start is None or end is None:
                continue
            out.append(
                CalendarWeek(
                    season_type=season_type,
                    week=week,
                    label=entry.get("label") or f"Week {week}",
                    start=start,
                    end=end,
                )
            )
    out.sort(key=lambda c: (c.start, c.season_type, c.week))
    return out


def current_week_from_payload(
    payload: Any, now: dt.datetime | None = None, season_type: int = SEASON_TYPE_REGULAR
) -> CalendarWeek | None:
    """The regular season week that now falls inside, else the next one to come.

    Reads ESPN's own calendar rather than doing date arithmetic, per the spec.
    """
    now = now or dt.datetime.now(dt.UTC)
    weeks = [c for c in parse_calendar(payload) if c.season_type == season_type]
    if not weeks:
        return None
    for entry in weeks:
        if entry.start <= now < entry.end:
            return entry
    upcoming = [c for c in weeks if c.start > now]
    if upcoming:
        return upcoming[0]
    return weeks[-1]  # the season is over, hold on the final week


def detect_current_week(
    db: Session,
    league: str = "nfl",
    year: int | None = None,
    now: dt.datetime | None = None,
) -> CalendarWeek | None:
    """Ask ESPN which week it is. Unmetered and cached for an hour."""
    segment, extra = _path(league)
    key = f"espn:calendar:{league}:{year or 'current'}"
    params: dict[str, Any] = dict(extra)
    if year:
        params["dates"] = year
    try:
        payload, _ = fetch_json(
            db,
            cache_key=key,
            url=f"{SITE_BASE}/{segment}/scoreboard",
            params=params,
            ttl_minutes=60,
        )
    except ProviderError:
        log.warning("could not detect the current week for %s", league)
        return None
    return current_week_from_payload(payload, now=now)
