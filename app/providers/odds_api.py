"""The Odds API spreads (Spec 5b). Metered, budgeted, and cached hard.

This is spread fallback number three in the resolution order of Spec 5e, used when ESPN has
no usable line for a game. Everything here is coded against a payload recorded from the live
API on 2026-08-02. A ten event excerpt of that capture is checked in as
tests/fixtures/odds_api_nfl_spreads.json.

Credit discipline is the whole point of the shape of this module:

* One request returns the whole upcoming league in a single response, so one call serves many
  weeks. Never fetch per game and never fetch per week.
* Cost is markets times regions, so regions=us with markets=spreads is exactly 1 credit.
* Every call goes through fetch_json with provider="odds_api" so it is counted against the
  monthly budget, short circuited by a fresh cache, and served stale when the feed is down.
  Nothing in this module touches httpx.

Verified field behaviour from the capture:

* The response is a JSON array of events, not an object.
* Each event is {id, sport_key, sport_title, commence_time (ISO Z), home_team, away_team,
  bookmakers: [{key, title, last_update, markets: [{key, last_update, outcomes: [...]}]}]}.
* Both team fields are full names ("Seattle Seahawks"), and outcomes[].name repeats one of
  them exactly.
* The outcome named after the home team carries the home relative spread directly in "point".
  Home "Seattle Seahawks" with point -3.5 means Seattle favoured by 3.5, which is already the
  sign convention the rest of the app uses. No flipping anywhere.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.providers.http import fetch_json
from app.providers.teams import MatchCandidate, canonical_key

log = logging.getLogger("picksportplus.odds_api")

BASE = "https://api.the-odds-api.com/v4"

# Our league key -> the sport key the feed expects.
SPORT_KEYS: dict[str, str] = {
    "nfl": "americanfootball_nfl",
    "ncaaf": "americanfootball_ncaaf",
}

# markets times regions. One market and one region is one credit.
CREDITS_PER_CALL = 1

SPREADS_MARKET = "spreads"

__all__ = [
    "SPORT_KEYS",
    "CREDITS_PER_CALL",
    "OddsApiGame",
    "sport_key_for",
    "median_home_spread",
    "parse_event",
    "parse_spreads",
    "fetch_spreads",
    "to_match_candidates",
]


@dataclass(frozen=True)
class OddsApiGame:
    event_id: str
    league: str
    kickoff: dt.datetime
    home_team: str
    away_team: str
    home_key: str
    away_key: str
    spread_home: float | None
    book_count: int


# Fetching -------------------------------------------------------------------


def sport_key_for(league: str) -> str:
    try:
        return SPORT_KEYS[league]
    except KeyError:
        raise ValueError(f"Unknown league {league!r}. Use nfl or ncaaf.") from None


def fetch_spreads(
    db: Session,
    league: str,
    ttl_minutes: int | None = None,
) -> tuple[list[OddsApiGame], str]:
    """Spreads for a whole league. Metered at 1 credit, so treat every call as expensive.

    Returns (games, source) where source is "live", "cache" or "stale". Propagates
    BudgetExceeded and ProviderError from the http layer rather than swallowing them, so the
    caller can fall back to ESPN or CFBD and the admin page can see why.
    """
    sport_key = sport_key_for(league)
    ttl = settings.spread_cache_minutes if ttl_minutes is None else ttl_minutes

    payload, source = fetch_json(
        db,
        cache_key=f"odds_api:spreads:{league}",
        url=f"{BASE}/sports/{sport_key}/odds",
        params={
            "apiKey": settings.odds_api_key,
            "regions": "us",
            "markets": SPREADS_MARKET,
            "oddsFormat": "american",
            "dateFormat": "iso",
        },
        provider="odds_api",
        credits=CREDITS_PER_CALL,
        ttl_minutes=ttl,
    )

    games = parse_spreads(payload, league)
    log.info("odds api %s: %d game(s) from %s", league, len(games), source)
    return games, source


# Parsing --------------------------------------------------------------------


def _parse_dt(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _as_float(raw: Any) -> float | None:
    """Numeric coercion that keeps a pick em at 0.0 and rejects booleans."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def _same_name(a: Any, b: Any) -> bool:
    """Outcome name against the event's home_team. The feed repeats it verbatim, but a
    difference in case or padding should not cost us a whole bookmaker."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a.strip().casefold() == b.strip().casefold()


def _home_point(bookmaker: Any, home_team: str) -> float | None:
    """The home relative point from one bookmaker, or None when it has nothing usable."""
    if not isinstance(bookmaker, dict):
        return None
    for market in bookmaker.get("markets") or []:
        if not isinstance(market, dict) or market.get("key") != SPREADS_MARKET:
            continue
        for outcome in market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            if _same_name(outcome.get("name"), home_team):
                return _as_float(outcome.get("point"))
    return None


def median_home_spread(event: Any) -> tuple[float | None, int]:
    """Median of the home team's point across all bookmakers offering a spreads market.

    Returns (spread, book_count). (None, 0) when nothing usable is present. The median is
    what stops one stale or outlier book from picking the slate, and an even number of books
    giving a midpoint is correct for lines: two books at -3 and -3.5 really is -3.25.
    """
    if not isinstance(event, dict):
        return None, 0
    home_team = event.get("home_team")
    if not isinstance(home_team, str) or not home_team.strip():
        return None, 0

    points = [
        point
        for point in (_home_point(book, home_team) for book in event.get("bookmakers") or [])
        if point is not None
    ]
    if not points:
        return None, 0
    return float(statistics.median(points)), len(points)


def parse_event(event: Any, league: str) -> OddsApiGame | None:
    """One event into an OddsApiGame. Returns None when it is unusable."""
    if not isinstance(event, dict):
        return None

    event_id = event.get("id")
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    if not isinstance(event_id, str) or not event_id:
        return None
    if not isinstance(home_team, str) or not home_team.strip():
        return None
    if not isinstance(away_team, str) or not away_team.strip():
        return None
    # Both sides named the same makes the home outcome lookup ambiguous, and an ambiguous
    # lookup would hand back a sign flipped line. A missing spread beats a wrong one.
    if _same_name(home_team, away_team):
        return None

    kickoff = _parse_dt(event.get("commence_time"))
    if kickoff is None:
        return None

    spread, book_count = median_home_spread(event)

    return OddsApiGame(
        event_id=event_id,
        league=league,
        kickoff=kickoff,
        home_team=home_team,
        away_team=away_team,
        home_key=canonical_key(home_team, league),
        away_key=canonical_key(away_team, league),
        spread_home=spread,
        book_count=book_count,
    )


def parse_spreads(payload: Any, league: str) -> list[OddsApiGame]:
    """Pure parse of the array. Never raises on a malformed event, it skips it.

    Sorted by kickoff then event id so the output is stable across calls, which keeps the
    admin review screen and the matching order deterministic.
    """
    if not isinstance(payload, list):
        # The feed always answers with a JSON array. Anything else means an error body was
        # served (and cached), and returning a silent empty list would be indistinguishable
        # from a league that genuinely has no lines yet.
        log.warning(
            "odds api %s payload is %s, not a list of events. No spreads from this response.",
            league,
            type(payload).__name__,
        )
        return []

    games: list[OddsApiGame] = []
    for event in payload:
        try:
            game = parse_event(event, league)
        except Exception as exc:  # one malformed event must not lose the league
            event_id = event.get("id") if isinstance(event, dict) else None
            log.warning("skipping unparsable odds api event %r: %s", event_id, exc)
            continue
        if game is not None:
            games.append(game)

    games.sort(key=lambda g: (g.kickoff, g.event_id))
    return games


# Matching -------------------------------------------------------------------


def to_match_candidates(games: Sequence[OddsApiGame]) -> list[MatchCandidate]:
    """Adapt to the shape app.providers.teams.match_by_teams_and_date expects.

    The payload is the OddsApiGame itself, so a matched candidate hands the caller the spread
    and the book count without a second lookup.
    """
    return [
        MatchCandidate(
            home_key=game.home_key,
            away_key=game.away_key,
            kickoff=game.kickoff,
            payload=game,
        )
        for game in games or []
    ]
