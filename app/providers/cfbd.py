"""CollegeFootballData lines. The college only, last resort spread source (Spec 5c, step 4 of 5e).

Metered. The free tier allows 1000 calls a month and app.config caps us well under that, so
every request here goes through fetch_json with provider="cfbd" and credits=1. One call covers
a whole week for every division, which is why the per week cap in app/services/ingest.py is a
single call. Never call this from a page render path.

Everything below is coded against a payload recorded from the live API on 2026-08-02, saved as
tests/fixtures/cfbd_lines_2025_w5.json (106 rows, 212 book lines). Three verified behaviours
that the rest of the app depends on:

1. The row "id" IS the ESPN event id. Cross checked against the recorded FBS scoreboard in
   tests/fixtures/espn_cfb_2025_w5.json: all 24 events in that capture found a CFBD row by id,
   every one of them carried a spread, and home versus away orientation agreed on all 24. So the
   join is an exact integer match on the event id, exposed here as a string to line up with
   Game.espn_event_id. There is no name matching in this module.

   Note the limit of that argument. CFBD also carries its own "homeTeamId" and "awayTeamId"
   namespace, so a second id space demonstrably exists in the same payload. The join is only
   safe while "id" stays in ESPN's namespace. home_team and away_team are carried on CfbdLine so
   a caller can confirm the matchup before trusting a joined spread, which Spec 5d asks for.
   Nothing confirms it today.
2. "spread" is already home relative, with a negative value meaning the home team is favoured,
   which is exactly our convention. Verified across all 212 recorded book lines against
   "formattedSpread" and against real finals. Home Arkansas, away Notre Dame, spread +5.5,
   formattedSpread "Notre Dame -5.5", final Notre Dame 56 Arkansas 13. Home Georgia, away
   Alabama, spread -2.5, formattedSpread "Georgia -2.5". The value is used as is and the sign
   is never flipped. As a guard against a future feed change, each line is cross checked against
   formattedSpread and any line whose sign disagrees is dropped rather than trusted.
3. The response covers every division, so FCS versus FCS rows are present. They are parsed and
   kept, and they simply never match an FBS ESPN event id, so they cost nothing.
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
from app.providers.teams import canonical_key

log = logging.getLogger("picksportplus.cfbd")

BASE = "https://api.collegefootballdata.com"
LINES_URL = f"{BASE}/lines"

# CFBD is college only, so every name here is canonicalised against the college table.
LEAGUE = "ncaaf"

# A week's lines stop moving once the games are played, so a live call is worth a whole week
# of reuse. This is the main reason the month's budget stays untouched.
LINES_TTL_MINUTES = 60 * 24 * 7


@dataclass(frozen=True)
class CfbdLine:
    """One CFBD row reduced to what the slate needs."""

    espn_event_id: str  # str(row["id"]), joins directly to Game.espn_event_id
    season: int
    week: int
    kickoff: dt.datetime | None  # timezone aware UTC
    home_team: str
    away_team: str
    spread_home: float | None  # home relative, negative means the home team is favoured
    book_count: int


# Parsing --------------------------------------------------------------------


def _parse_dt(raw: Any) -> dt.datetime | None:
    """CFBD sends "2025-09-27T16:00:00.000Z", so milliseconds plus a Z suffix."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _number(raw: Any) -> float | None:
    """A float from a number or a numeric string. Booleans are not numbers here."""
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


def _int_or_zero(raw: Any) -> int:
    value = _number(raw)
    return int(value) if value is not None else 0


def _text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _home_relative_from_formatted(row: dict, formatted: Any) -> float | None:
    """Read formattedSpread ("Notre Dame -5.5") as a home relative number.

    Returns None when the string cannot be read or names neither side, in which case there is
    nothing to cross check against and the raw spread is taken at face value.
    """
    text = _text(formatted)
    if not text:
        return None
    name, _, tail = text.rpartition(" ")
    value = _number(tail)
    if value is None or not name.strip():
        return None

    home_key = canonical_key(_text(row.get("homeTeam")), LEAGUE)
    away_key = canonical_key(_text(row.get("awayTeam")), LEAGUE)
    if home_key == away_key:
        return None

    named = canonical_key(name.strip(), LEAGUE)
    if named == home_key:
        # "Georgia -2.5" with Georgia at home is a home relative -2.5.
        return value
    if named == away_key:
        # "Notre Dame -5.5" with Notre Dame away is a home relative +5.5.
        return -value
    return None


def _line_spread(row: dict, line: Any) -> float | None:
    """The home relative spread from one book entry, or None when it is not trustworthy."""
    if not isinstance(line, dict):
        return None
    spread = _number(line.get("spread"))
    if spread is None:
        return None

    expected = _home_relative_from_formatted(row, line.get("formattedSpread"))
    if expected is not None and (spread > 0 > expected or spread < 0 < expected):
        # Never seen in the recorded capture. If it ever fires, CFBD changed its convention and
        # a missing spread is far better than a backwards one.
        log.warning(
            "dropping %s line for event %s: spread %s disagrees with %r",
            line.get("provider"),
            row.get("id"),
            spread,
            line.get("formattedSpread"),
        )
        return None
    return spread


def median_home_spread(row: dict) -> tuple[float | None, int]:
    """Median of row["lines"][*]["spread"], home relative.

    Nulls and any entry whose formattedSpread contradicts the sign are skipped. Returns
    (spread, book_count) where book_count counts only the books that contributed.
    """
    if not isinstance(row, dict):
        return None, 0
    values = [
        value
        for value in (_line_spread(row, line) for line in row.get("lines") or [])
        if value is not None
    ]
    if not values:
        return None, 0
    return float(statistics.median(values)), len(values)


def parse_row(row: Any) -> CfbdLine | None:
    """One CFBD row into a CfbdLine. Returns None when it is unusable."""
    if not isinstance(row, dict):
        return None

    raw_id = row.get("id")
    if raw_id is None or isinstance(raw_id, bool):
        return None
    event_id = str(raw_id).strip()
    if not event_id:
        return None

    home_team = _text(row.get("homeTeam"))
    away_team = _text(row.get("awayTeam"))
    if not home_team or not away_team:
        return None

    spread, book_count = median_home_spread(row)

    return CfbdLine(
        espn_event_id=event_id,
        season=_int_or_zero(row.get("season")),
        week=_int_or_zero(row.get("week")),
        kickoff=_parse_dt(row.get("startDate")),
        home_team=home_team,
        away_team=away_team,
        spread_home=spread,
        book_count=book_count,
    )


def parse_lines(payload: Any) -> list[CfbdLine]:
    """Pure parse of the /lines array. Skips malformed rows rather than raising.

    Rows with no usable book line are still returned, with spread_home None, so the caller can
    see that CFBD knew about the game. Callers must check spread_home before using it.
    """
    if not isinstance(payload, list):
        return []
    out: list[CfbdLine] = []
    for row in payload:
        try:
            parsed = parse_row(row)
        except Exception as exc:  # one bad row must not lose the week
            log.warning("skipping unparsable cfbd row: %s", exc)
            continue
        if parsed is not None:
            out.append(parsed)
    return out


def lines_by_event_id(lines: Sequence[CfbdLine]) -> dict[str, CfbdLine]:
    """Index for the exact ESPN event id join.

    Ids are unique in the feed. If a duplicate ever appears, the entry backed by more books
    wins, and a tie keeps the first, so the result does not depend on row order.
    """
    index: dict[str, CfbdLine] = {}
    for line in lines or []:
        existing = index.get(line.espn_event_id)
        if existing is None or line.book_count > existing.book_count:
            index[line.espn_event_id] = line
    return index


# Fetching -------------------------------------------------------------------


def fetch_lines(
    db: Session,
    year: int,
    week: int,
    season_type: str = "regular",
) -> tuple[list[CfbdLine], str]:
    """Lines for one college week. Metered, one credit, college only, last resort.

    Returns (lines, source) where source is "live", "cache" or "stale". Propagates
    BudgetExceeded and ProviderError so the caller decides what to tell the admin, and so a
    spent budget is never mistaken for a week with no lines.
    """
    key = f"cfbd:lines:{year}:{season_type}:{week}"
    payload, source = fetch_json(
        db,
        cache_key=key,
        url=LINES_URL,
        params={"year": year, "week": week, "seasonType": season_type},
        headers={"Authorization": f"Bearer {settings.cfbd_api_key}"},
        provider="cfbd",
        credits=1,
        ttl_minutes=LINES_TTL_MINUTES,
    )
    lines = parse_lines(payload)
    # A row with no usable spread is either a game CFBD has not priced or one whose books all
    # failed the formattedSpread sign check. Either way the caller sees only a missing spread,
    # so the count is logged here to keep a feed convention change from being invisible.
    unpriced = sum(1 for line in lines if line.spread_home is None)
    log.info(
        "cfbd lines %s served from %s, %d rows, %d without a usable spread",
        key,
        source,
        len(lines),
        unpriced,
    )
    return lines, source
