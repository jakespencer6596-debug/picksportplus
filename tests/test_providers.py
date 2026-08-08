"""Provider tests for app/providers/espn.py, against recorded fixtures only.

Every payload used here was captured from the live ESPN API on 2026-08-02 and is
replayed from tests/fixtures. Nothing in this module opens a socket, and the autouse
offline fixture in conftest.py makes that impossible anyway.

Three things are load bearing for the rest of the app and are pinned here:

1. The scoreboard drops competitions[0].odds once a game is completed, so a finished
   week carries no spread at all and has to fall back to the core API (Spec 5a, 5e).
2. Spec 5a says not to trust the raw sign of odds["spread"], so the parser derives the
   home relative number from the favourite booleans plus the magnitude, with the
   "ABBR -X.X" details string as the last resort. Note that in every payload recorded
   so far the raw sign does agree, so that rule is defensive, not a recorded failure.
   See test_every_recorded_raw_spread_sign_happens_to_agree.
3. ESPN is keyless and unmetered. Every ESPN request must go through fetch_json with
   provider left as None and a ttl_minutes set, so it can never touch the metered
   budget and repeat calls inside the ttl cost nothing. The "Credit discipline"
   section pins the exact fetch_json keywords for all three call sites.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import datetime as dt
import inspect
import pathlib
import re
from typing import Any

import pytest

from app.config import settings
from app.models import ProviderUsage
from app.providers import espn
from app.providers import http as http_mod
from app.providers.http import ProviderError, cache_put

NFL_W5 = "espn_nfl_2025_w5.json"
CFB_W5 = "espn_cfb_2025_w5.json"
NFL_UPCOMING = "espn_nfl_upcoming_with_odds.json"
NFL_CORE_ODDS = "espn_core_odds_nfl_2025_w5.json"

# San Francisco at the Los Angeles Rams, Thursday night of week 5, final 26 to 23.
SF_AT_LAR = "401772939"
UPCOMING_EVENT = "401873271"

RECORD_RE = re.compile(r"^\d+-\d+(-\d+)?$")

UTC = dt.UTC


# Helpers --------------------------------------------------------------------


def _competitor(home_away: str, abbr: str, name: str, score: str | None, **extra) -> dict:
    competitor: dict = {
        "homeAway": home_away,
        "team": {"abbreviation": abbr, "displayName": name},
        "records": [{"type": "total", "summary": "1-1"}],
    }
    if score is not None:
        competitor["score"] = score
    competitor.update(extra)
    return competitor


def _synthetic_event(
    *,
    name: str,
    completed: bool = False,
    state: str = "pre",
    home_score: str | None = "21",
    away_score: str | None = "17",
    date: str = "2026-09-13T17:00Z",
    home_extra: dict | None = None,
    away_extra: dict | None = None,
    odds: list[dict] | None = None,
) -> dict:
    """A minimal scoreboard event, shaped like the real ones but hand built."""
    return {
        "id": "999999",
        "date": date,
        "status": {"type": {"name": name, "completed": completed, "state": state}},
        "competitions": [
            {
                "neutralSite": False,
                "odds": odds or [],
                "competitors": [
                    _competitor(
                        "home", "KC", "Kansas City Chiefs", home_score, **(home_extra or {})
                    ),
                    _competitor("away", "DEN", "Denver Broncos", away_score, **(away_extra or {})),
                ],
            }
        ],
    }


# Scoreboard parsing, completed NFL week -------------------------------------


def test_nfl_week5_parses_fourteen_final_games(load_fixture):
    games = espn.parse_scoreboard(load_fixture(NFL_W5), "nfl")

    assert len(games) == 14
    assert {g.status for g in games} == {"final"}
    assert all(g.is_final for g in games)
    assert {g.winner for g in games} <= {"home", "away", "tie"}
    assert all(g.winner is not None for g in games)
    for game in games:
        assert isinstance(game.home.score, int)
        assert isinstance(game.away.score, int)
        assert not isinstance(game.home.score, bool)


def test_nfl_week5_event_ids_are_unique_strings(load_fixture):
    games = espn.parse_scoreboard(load_fixture(NFL_W5), "nfl")

    ids = [g.event_id for g in games]
    assert len(set(ids)) == len(ids)
    assert all(isinstance(i, str) and i.isdigit() for i in ids)
    assert SF_AT_LAR in ids


def test_sf_at_lar_is_an_away_win_26_to_23(load_fixture):
    games = espn.parse_scoreboard(load_fixture(NFL_W5), "nfl")
    game = next(g for g in games if g.event_id == SF_AT_LAR)

    assert game.league == "nfl"
    assert game.status == "final"
    assert game.winner == "away"
    assert game.away.abbr == "SF"
    assert game.away.score == 26
    assert game.home.abbr == "LAR"
    assert game.home.score == 23
    assert game.away.name == "San Francisco 49ers"
    assert game.home.name == "Los Angeles Rams"
    assert game.away.canonical == "nfl:san-francisco-49ers"
    assert game.home.canonical == "nfl:los-angeles-rams"
    assert game.home.record == "3-2"
    assert game.away.record == "4-1"
    assert game.neutral_site is False


def test_team_fields_are_populated_for_every_nfl_game(load_fixture):
    games = espn.parse_scoreboard(load_fixture(NFL_W5), "nfl")

    for game in games:
        for side in (game.home, game.away):
            assert side.abbr and side.abbr == side.abbr.upper()
            assert 1 <= len(side.abbr) <= 5
            assert side.name and side.name != "Unknown"
            assert side.canonical.startswith("nfl:")
            assert side.canonical != "nfl:unknown"
            assert RECORD_RE.match(side.record or ""), side.record


def test_kickoffs_are_timezone_aware_utc(load_fixture):
    games = espn.parse_scoreboard(load_fixture(NFL_W5), "nfl")

    for game in games:
        assert game.kickoff.tzinfo is not None
        assert game.kickoff.utcoffset() == dt.timedelta(0)

    kickoff = next(g for g in games if g.event_id == SF_AT_LAR).kickoff
    assert kickoff == dt.datetime(2025, 10, 3, 0, 15, tzinfo=UTC)


def test_completed_week_carries_no_spread_at_all(load_fixture):
    """ESPN drops competitions[0].odds once a game is final. This is the reason the
    core API fallback exists, so it is pinned rather than left implicit."""
    payload = load_fixture(NFL_W5)

    raw_odds = [event["competitions"][0].get("odds") for event in payload["events"]]
    assert all(not odds for odds in raw_odds)

    games = espn.parse_scoreboard(payload, "nfl")
    assert [g.spread_home for g in games] == [None] * 14
    assert [g.spread_source for g in games] == [None] * 14


# Scoreboard parsing, college -------------------------------------------------


def test_cfb_week5_parses_twenty_four_final_games_with_ncaaf_keys(load_fixture):
    games = espn.parse_scoreboard(load_fixture(CFB_W5), "ncaaf")

    assert len(games) == 24
    assert {g.status for g in games} == {"final"}
    assert {g.league for g in games} == {"ncaaf"}
    assert all(g.home.canonical.startswith("ncaaf:") for g in games)
    assert all(g.away.canonical.startswith("ncaaf:") for g in games)
    assert not any(g.home.canonical.endswith(":unknown") for g in games)
    assert not any(g.away.canonical.endswith(":unknown") for g in games)
    # Completed college games have no scoreboard odds either.
    assert all(g.spread_home is None for g in games)


# Scoreboard parsing, upcoming game with odds --------------------------------


def test_upcoming_game_yields_a_home_relative_spread(load_fixture):
    """CAR at ARI with details "CAR -1.5". The away team is favoured by a point and a
    half, so the home relative number is positive."""
    games = espn.parse_scoreboard(load_fixture(NFL_UPCOMING), "nfl")

    assert len(games) == 1
    game = games[0]
    assert game.event_id == UPCOMING_EVENT
    assert game.away.abbr == "CAR"
    assert game.home.abbr == "ARI"
    assert game.status == "scheduled"
    assert game.winner is None
    assert game.spread_home == 1.5
    assert game.spread_source == "espn"
    assert game.neutral_site is True


def test_upcoming_fixture_details_string_is_the_one_we_claim(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    odds = payload["events"][0]["competitions"][0]["odds"]

    assert odds[0]["details"] == "CAR -1.5"
    assert odds[0]["awayTeamOdds"]["favorite"] is True
    assert odds[0]["homeTeamOdds"]["favorite"] is False
    # The raw spread here is +1.5, which is already the home relative number because
    # the away team is favoured. The parser still ignores the raw sign, per Spec 5a.
    assert odds[0]["spread"] == 1.5


def test_every_recorded_raw_spread_sign_happens_to_agree(load_fixture):
    """Honest note about the "do not trust the raw sign" rule.

    Spec 5a says to derive the sign from the favourite booleans. Across every odds
    entry recorded so far, both scoreboard and core API, the raw sign already matches
    the home relative number, so no fixture demonstrates the disagreement. The rule is
    defensive. This test states that plainly rather than letting the module docstring
    imply a failure the recordings do not contain. If a future capture does disagree,
    this test goes red and the recording becomes the real evidence.
    """
    entries: list[dict] = []
    for entry in load_fixture(NFL_CORE_ODDS).values():
        entries.extend(entry["items"])
    entries.extend(load_fixture(NFL_UPCOMING)["events"][0]["competitions"][0]["odds"])
    assert len(entries) == 29

    for item in entries:
        magnitude = abs(float(item["spread"]))
        home_relative = -magnitude if item["homeTeamOdds"]["favorite"] else magnitude
        assert home_relative == item["spread"], item["details"]


# parse_spread_item -----------------------------------------------------------


def test_parse_spread_item_home_favourite_is_negative():
    item = {
        "details": "KC -3.5",
        "spread": 3.5,
        "homeTeamOdds": {"favorite": True},
        "awayTeamOdds": {"favorite": False},
    }
    assert espn.parse_spread_item(item, "KC", "DEN") == -3.5


def test_parse_spread_item_away_favourite_is_positive():
    item = {
        "details": "DEN -6.5",
        "spread": -6.5,
        "homeTeamOdds": {"favorite": False},
        "awayTeamOdds": {"favorite": True},
    }
    # The raw sign is ignored. The away team is favoured, so home is the underdog.
    assert espn.parse_spread_item(item, "KC", "DEN") == 6.5


@pytest.mark.parametrize("details", ["EVEN", "even", "PK", "pk", "Pick", "pick em", "PICKEM"])
def test_parse_spread_item_pick_em_is_zero(details):
    assert espn.parse_spread_item({"details": details}, "KC", "DEN") == 0.0


def test_parse_spread_item_zero_magnitude_is_zero():
    item = {"details": "", "spread": 0, "homeTeamOdds": {}, "awayTeamOdds": {}}
    assert espn.parse_spread_item(item, "KC", "DEN") == 0.0


def test_parse_spread_item_details_only_fallback_for_home_team():
    assert espn.parse_spread_item({"details": "KC -3.5"}, "KC", "DEN") == -3.5


def test_parse_spread_item_details_only_fallback_for_away_team():
    assert espn.parse_spread_item({"details": "KC -3.5"}, "DEN", "KC") == 3.5


def test_parse_spread_item_details_fallback_is_case_insensitive():
    assert espn.parse_spread_item({"details": "kc -3.5"}, "KC", "DEN") == -3.5


def test_parse_spread_item_falls_back_when_no_favourite_flag_is_set():
    item = {"details": "KC -7", "spread": 7, "homeTeamOdds": {}, "awayTeamOdds": {}}
    assert espn.parse_spread_item(item, "KC", "DEN") == -7.0


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"details": ""},
        {"details": "Off"},
        {"details": "TBD -3.5"},  # abbreviation belongs to neither side
        {"details": "KC nonsense"},
        {"spread": None, "details": None},
        {"details": "KC -3.5", "spread": "not a number"},
    ],
)
def test_parse_spread_item_returns_none_when_nothing_is_trustworthy(item):
    # "KC -3.5" with a junk spread still resolves through the details fallback, so it
    # is excluded from this list by giving it an abbreviation neither side owns.
    if item.get("details") == "KC -3.5":
        item = dict(item, details="ZZZ -3.5")
    assert espn.parse_spread_item(item, "KC", "DEN") is None


def test_parse_spread_item_tolerates_missing_abbreviations():
    assert espn.parse_spread_item({"details": "KC -3.5"}, "", "") is None


# spread_from_items -----------------------------------------------------------


def test_spread_from_items_takes_the_median_across_books():
    items = [
        {"details": "KC -3", "spread": 3, "homeTeamOdds": {"favorite": True}},
        {"details": "KC -4", "spread": 4, "homeTeamOdds": {"favorite": True}},
        {"details": "KC -3.5", "spread": 3.5, "homeTeamOdds": {"favorite": True}},
    ]
    assert espn.spread_from_items(items, "KC", "DEN") == -3.5


def test_spread_from_items_ignores_unparsable_entries():
    items = [
        {"details": "KC -3", "spread": 3, "homeTeamOdds": {"favorite": True}},
        {"details": "Off"},
        {},
        {"details": "KC -4", "spread": 4, "homeTeamOdds": {"favorite": True}},
        {"details": "ZZZ -99"},
    ]
    assert espn.spread_from_items(items, "KC", "DEN") == -3.5


def test_spread_from_items_is_a_median_not_a_mean():
    items = [
        {"details": "KC -3", "spread": 3, "homeTeamOdds": {"favorite": True}},
        {"details": "KC -3", "spread": 3, "homeTeamOdds": {"favorite": True}},
        {"details": "KC -30", "spread": 30, "homeTeamOdds": {"favorite": True}},
    ]
    assert espn.spread_from_items(items, "KC", "DEN") == -3.0


@pytest.mark.parametrize("items", [[], None, [{}], [{"details": "Off"}, {"details": ""}]])
def test_spread_from_items_returns_none_without_a_usable_book(items):
    assert espn.spread_from_items(items, "KC", "DEN") is None


def test_spread_from_items_always_returns_a_float():
    items = [{"details": "KC -3", "spread": 3, "homeTeamOdds": {"favorite": True}}]
    value = espn.spread_from_items(items, "KC", "DEN")
    assert isinstance(value, float)


# parse_moneyline_item / moneyline_from_items (Phase 8) ------------------------
#
# Spec Section 5a documents the odds object as carrying details, spread, overUnder, and
# homeTeamOdds/awayTeamOdds each with only a favorite boolean, nothing more, and every
# fixture recorded in tests/fixtures for this codebase agrees: none of them ever carry a
# moneyline anywhere (checked directly, see DECISIONS.md, Phase 8). The items below are
# NOT recordings of a real ESPN response. They are hand built, in the same shape
# test_providers.py's own _synthetic_event already uses for a payload this codebase has
# never actually observed, to prove the parsing path works IF a real payload ever does
# carry a moneyline shaped key, without pretending any of this was seen live.


def test_parse_moneyline_item_reads_a_synthetic_moneyline():
    # Synthetic: no recorded ESPN payload in this codebase carries a moneyLine key.
    item = {
        "details": "KC -3.5",
        "spread": 3.5,
        "homeTeamOdds": {"favorite": True, "moneyLine": -180},
        "awayTeamOdds": {"favorite": False, "moneyLine": 155},
    }
    assert espn.parse_moneyline_item(item) == (-180, 155)


def test_parse_moneyline_item_missing_key_returns_none_none():
    item = {"details": "KC -3.5", "spread": 3.5, "homeTeamOdds": {"favorite": True}}
    assert espn.parse_moneyline_item(item) == (None, None)


def test_parse_moneyline_item_tolerates_a_non_numeric_value():
    # Synthetic: a malformed moneyline should not raise, just come back as missing.
    item = {"homeTeamOdds": {"moneyLine": "N/A"}, "awayTeamOdds": {"moneyLine": 155}}
    assert espn.parse_moneyline_item(item) == (None, 155)


def test_moneyline_from_items_takes_the_first_book_with_both_sides():
    # Synthetic: two bookmakers, the first with only one side, the second with both.
    items = [
        {"homeTeamOdds": {"moneyLine": -180}, "awayTeamOdds": {}},
        {"homeTeamOdds": {"moneyLine": -175}, "awayTeamOdds": {"moneyLine": 150}},
    ]
    assert espn.moneyline_from_items(items) == (-175, 150)


@pytest.mark.parametrize("items", [[], None, [{}], [{"homeTeamOdds": {}, "awayTeamOdds": {}}]])
def test_moneyline_from_items_returns_none_none_without_a_usable_book(items):
    assert espn.moneyline_from_items(items) == (None, None)


def test_recorded_upcoming_fixture_has_no_moneyline(load_fixture):
    """The real recording: confirms the documented reality that this codebase's data never
    carries a moneyline, so parse_event's home_moneyline/away_moneyline come back None on
    live-shaped traffic today (app.scenarios.win_probability then falls back to spread)."""
    payload = load_fixture(NFL_UPCOMING)
    games = espn.parse_scoreboard(payload, "nfl")
    assert games
    for game in games:
        assert game.home_moneyline is None
        assert game.away_moneyline is None


def test_parse_event_reads_moneyline_when_a_synthetic_payload_carries_one():
    event = _synthetic_event(
        name="STATUS_SCHEDULED",
        odds=[
            {
                "details": "KC -3.5",
                "spread": 3.5,
                "homeTeamOdds": {"favorite": True, "moneyLine": -180},
                "awayTeamOdds": {"favorite": False, "moneyLine": 155},
            }
        ],
    )
    game = espn.parse_event(event, "nfl")
    assert game.home_moneyline == -180
    assert game.away_moneyline == 155


# parse_core_odds -------------------------------------------------------------


def _pregame_only(entry: dict) -> dict:
    """Drop the live in-game book, keeping the pregame line.

    The core API returns two entries per event: the pregame "ESPN BET" line and an
    "ESPN Bet - Live Odds" line that was captured while the game was being played.
    See the note in the module report about why mixing the two is a problem.
    """
    items = [
        item
        for item in entry.get("items") or []
        if "live" not in ((item.get("provider") or {}).get("name") or "").lower()
    ]
    return {"items": items}


def test_core_odds_fixture_is_keyed_by_event_id(load_fixture):
    core = load_fixture(NFL_CORE_ODDS)

    assert set(core) >= {SF_AT_LAR, "401772814", "401772746"}
    for entry in core.values():
        assert entry["count"] == len(entry["items"]) == 2


def test_parse_core_odds_sf_at_lar_pregame_line_is_lar_minus_8_5(load_fixture):
    """Event 401772939, home LAR favoured by 8.5, so the home relative spread is -8.5."""
    entry = load_fixture(NFL_CORE_ODDS)[SF_AT_LAR]

    assert entry["items"][0]["details"] == "LAR -8.5"
    assert espn.parse_core_odds(_pregame_only(entry), "LAR", "SF") == -8.5
    assert espn.parse_spread_item(entry["items"][0], "LAR", "SF") == -8.5


def test_parse_core_odds_kc_at_jax_matches_its_details_string(load_fixture):
    """Event 401772814. Both recorded books say "KC -3.5" and KC is the away team, so
    the home relative spread is +3.5 either way."""
    entry = load_fixture(NFL_CORE_ODDS)["401772814"]

    assert [item["details"] for item in entry["items"]] == ["KC -3.5", "KC -3.5"]
    assert espn.parse_core_odds(entry, "JAX", "KC") == 3.5
    assert espn.parse_core_odds(_pregame_only(entry), "JAX", "KC") == 3.5


def test_parse_core_odds_tb_at_sea_matches_its_details_string(load_fixture):
    """Event 401772746. Both books favour home SEA, by 3.5 and 2.5, median -3.0."""
    entry = load_fixture(NFL_CORE_ODDS)["401772746"]

    assert [item["details"] for item in entry["items"]] == ["SEA -3.5", "SEA -2.5"]
    assert espn.parse_core_odds(entry, "SEA", "TB") == -3.0
    assert espn.parse_core_odds(_pregame_only(entry), "SEA", "TB") == -3.5


def test_parse_core_odds_pregame_lines_match_every_recorded_details_string(load_fixture):
    """The pregame book alone always agrees in sign with its own details string."""
    core = load_fixture(NFL_CORE_ODDS)
    games = {g.event_id: g for g in espn.parse_scoreboard(load_fixture(NFL_W5), "nfl")}

    for event_id, entry in core.items():
        game = games[event_id]
        pregame = entry["items"][0]
        value = espn.parse_core_odds(_pregame_only(entry), game.home.abbr, game.away.abbr)
        favoured_abbr, magnitude = pregame["details"].split()
        assert value is not None
        assert abs(value) == abs(float(magnitude))
        if favoured_abbr == game.home.abbr:
            assert value < 0
        else:
            assert value > 0


def test_parse_core_odds_median_is_dragged_by_the_live_in_game_book(load_fixture):
    """Known defect, pinned so the fix is visible when it lands.

    The core API entry mixes a pregame line with a line taken during play. Taking the
    median across both produces a number that was never a real pregame spread. For
    LV at IND the pregame line was IND -7.5 and the live line was IND -31.5 during a
    40 to 6 rout, and the median is -19.5. See the report for the suggested filter.
    """
    core = load_fixture(NFL_CORE_ODDS)

    assert espn.parse_core_odds(core[SF_AT_LAR], "LAR", "SF") == -3.5
    assert espn.parse_core_odds(core["401772851"], "IND", "LV") == -19.5
    assert espn.parse_core_odds(core["401772853"], "LAC", "WSH") == 3.5


@pytest.mark.parametrize("payload", [None, [], "nope", 42, {}, {"items": []}])
def test_parse_core_odds_returns_none_for_unusable_payloads(payload):
    assert espn.parse_core_odds(payload, "KC", "DEN") is None


def test_parse_core_odds_ignores_the_top_level_dict_of_events(load_fixture):
    """The fixture file is a dict keyed by event id, not a single core response. The
    parser expects one event's response, so handing it the whole file yields nothing."""
    assert espn.parse_core_odds(load_fixture(NFL_CORE_ODDS), "LAR", "SF") is None


# Calendar and week detection -------------------------------------------------


def test_calendar_payload_has_four_season_type_groups(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    groups = payload["leagues"][0]["calendar"]

    assert len(groups) == 4
    assert [g["label"] for g in groups] == [
        "Preseason",
        "Regular Season",
        "Postseason",
        "Off Season",
    ]
    # The off season group omits "entries" entirely, so it contributes no weeks.
    assert [len(g.get("entries") or []) for g in groups] == [4, 18, 5, 0]
    assert "entries" not in groups[3]


def test_parse_calendar_flattens_every_group_that_has_entries(load_fixture):
    weeks = espn.parse_calendar(load_fixture(NFL_UPCOMING))

    assert len(weeks) == 4 + 18 + 5
    assert {w.season_type for w in weeks} == {1, 2, 3}
    regular = [w for w in weeks if w.season_type == espn.SEASON_TYPE_REGULAR]
    assert len(regular) == 18
    assert [w.week for w in regular] == list(range(1, 19))
    assert regular[0].label == "Week 1"
    assert regular[-1].label == "Week 18"


def test_parse_calendar_entries_are_sorted_and_timezone_aware(load_fixture):
    weeks = espn.parse_calendar(load_fixture(NFL_UPCOMING))

    assert weeks == sorted(weeks, key=lambda w: (w.start, w.season_type, w.week))
    for week in weeks:
        assert week.start.tzinfo is not None
        assert week.end.tzinfo is not None
        assert week.start.utcoffset() == dt.timedelta(0)
        assert week.end.utcoffset() == dt.timedelta(0)
        assert week.start < week.end

    first_regular = next(w for w in weeks if w.season_type == espn.SEASON_TYPE_REGULAR)
    assert first_regular.start == dt.datetime(2026, 9, 9, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize("payload", [None, {}, [], {"leagues": []}, {"leagues": [{}]}])
def test_parse_calendar_returns_empty_for_unusable_payloads(payload):
    assert espn.parse_calendar(payload) == []


def test_current_week_inside_a_known_week(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    now = dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)

    week = espn.current_week_from_payload(payload, now=now)

    assert week is not None
    assert week.season_type == espn.SEASON_TYPE_REGULAR
    assert week.week == 2
    assert week.start <= now < week.end


def test_current_week_on_the_exact_start_boundary(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    now = dt.datetime(2026, 9, 9, 7, 0, tzinfo=UTC)

    week = espn.current_week_from_payload(payload, now=now)

    assert week is not None
    assert week.week == 1


def test_current_week_before_the_season_returns_the_first_upcoming_week(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    # Early August, the preseason is running but the regular season has not started.
    now = dt.datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    week = espn.current_week_from_payload(payload, now=now)

    assert week is not None
    assert week.week == 1
    assert week.start > now


def test_current_week_after_the_season_holds_on_the_last_week(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    now = dt.datetime(2027, 6, 1, 12, 0, tzinfo=UTC)

    week = espn.current_week_from_payload(payload, now=now)

    assert week is not None
    assert week.week == 18
    assert week.end < now


def test_current_week_can_be_asked_for_the_postseason(load_fixture):
    payload = load_fixture(NFL_UPCOMING)
    now = dt.datetime(2027, 1, 15, 12, 0, tzinfo=UTC)

    week = espn.current_week_from_payload(payload, now=now, season_type=espn.SEASON_TYPE_POSTSEASON)

    assert week is not None
    assert week.season_type == espn.SEASON_TYPE_POSTSEASON
    assert week.label == "Wild Card"


def test_current_week_returns_none_without_a_calendar():
    assert espn.current_week_from_payload({"leagues": [{}]}) is None


# Status mapping --------------------------------------------------------------


def test_status_final_comes_from_the_completed_flag(load_fixture):
    event = load_fixture(NFL_W5)["events"][0]
    assert event["status"]["type"]["completed"] is True

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.status == "final"


@pytest.mark.parametrize(
    ("name", "completed", "state", "expected"),
    [
        ("STATUS_CANCELED", False, "post", "void"),
        ("STATUS_POSTPONED", False, "pre", "void"),
        ("STATUS_SCHEDULED", False, "pre", "scheduled"),
        ("STATUS_IN_PROGRESS", False, "in", "in_progress"),
        ("STATUS_HALFTIME", False, "in", "in_progress"),
        ("STATUS_FINAL", True, "post", "final"),
        # Over but not completed, for example a forfeit. There is no trustworthy
        # winner, so it is void rather than final.
        ("STATUS_FORFEIT", False, "post", "void"),
    ],
)
def test_status_mapping_through_parse_event(name, completed, state, expected):
    event = _synthetic_event(name=name, completed=completed, state=state)

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.status == expected


def test_canceled_game_carries_no_winner():
    event = _synthetic_event(name="STATUS_CANCELED", completed=False, state="post")

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.winner is None


def test_final_tie_is_reported_as_a_tie():
    event = _synthetic_event(
        name="STATUS_FINAL", completed=True, state="post", home_score="21", away_score="21"
    )

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.winner == "tie"


def test_final_without_scores_falls_back_to_the_winner_flag():
    event = _synthetic_event(
        name="STATUS_FINAL",
        completed=True,
        state="post",
        home_score=None,
        away_score=None,
        home_extra={"winner": True},
        away_extra={"winner": False},
    )

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.home.score is None
    assert game.winner == "home"


def test_scheduled_game_has_no_winner_even_with_zero_scores():
    event = _synthetic_event(
        name="STATUS_SCHEDULED", completed=False, state="pre", home_score="0", away_score="0"
    )

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.status == "scheduled"
    assert game.winner is None


def test_synthetic_event_still_resolves_canonical_keys():
    game = espn.parse_event(_synthetic_event(name="STATUS_SCHEDULED"), "nfl")

    assert game is not None
    assert game.home.canonical == "nfl:kansas-city-chiefs"
    assert game.away.canonical == "nfl:denver-broncos"


# Malformed input -------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {"id": "1", "date": "2025-10-05T17:00Z"},  # no competitions
        {"id": "2", "date": "2025-10-05T17:00Z", "competitions": []},
        {"id": "3", "date": "2025-10-05T17:00Z", "competitions": [{"competitors": []}]},
        {  # only one side
            "id": "4",
            "date": "2025-10-05T17:00Z",
            "competitions": [
                {"competitors": [_competitor("home", "KC", "Kansas City Chiefs", "10")]}
            ],
        },
        {  # unparsable kickoff
            "id": "5",
            "date": "the fifth of never",
            "competitions": [
                {
                    "competitors": [
                        _competitor("home", "KC", "Kansas City Chiefs", "10"),
                        _competitor("away", "DEN", "Denver Broncos", "7"),
                    ]
                }
            ],
        },
        {  # no kickoff at all
            "id": "6",
            "competitions": [
                {
                    "competitors": [
                        _competitor("home", "KC", "Kansas City Chiefs", "10"),
                        _competitor("away", "DEN", "Denver Broncos", "7"),
                    ]
                }
            ],
        },
    ],
)
def test_parse_event_returns_none_for_a_malformed_event(event):
    assert espn.parse_event(event, "nfl") is None


def test_one_malformed_event_does_not_lose_the_rest_of_the_week(load_fixture):
    payload = copy.deepcopy(load_fixture(NFL_W5))
    good = len(payload["events"])
    assert good == 14

    payload["events"] = [
        {"id": "bad-1", "date": "2025-10-05T17:00Z"},
        payload["events"][0],
        "not even a dict",
        payload["events"][1],
        {"id": "bad-2", "date": "nope", "competitions": [{"competitors": []}]},
        *payload["events"][2:],
        None,
    ]

    games = espn.parse_scoreboard(payload, "nfl")

    assert len(games) == good
    assert {g.event_id for g in games} == {e["id"] for e in load_fixture(NFL_W5)["events"]}


@pytest.mark.parametrize("payload", [None, [], "nope", 7, {}, {"events": None}])
def test_parse_scoreboard_returns_empty_for_unusable_payloads(payload):
    assert espn.parse_scoreboard(payload, "nfl") == []


def test_unknown_team_names_still_produce_a_game():
    """A team ESPN has never sent before must not blow up the week. It gets a slugged
    fallback key, which the matcher will refuse to pair, which is the safe outcome."""
    event = _synthetic_event(name="STATUS_SCHEDULED")
    event["competitions"][0]["competitors"][0]["team"] = {
        "abbreviation": "ZZZ",
        "displayName": "Zamboni City Icebreakers",
    }

    game = espn.parse_event(event, "nfl")

    assert game is not None
    assert game.home.abbr == "ZZZ"
    assert game.home.canonical == "nfl:zamboni-city-icebreakers"


# Public contract -------------------------------------------------------------
#
# app/services/ingest.py and app/services/results.py call these positionally, so the
# names, the order and the defaults are the contract, not an implementation detail.


def _params(fn) -> list[tuple[str, str, Any]]:
    return [
        (p.name, p.kind.name, "REQUIRED" if p.default is p.empty else p.default)
        for p in inspect.signature(fn).parameters.values()
    ]


def test_espn_game_field_names_and_order_are_stable():
    fields = dataclasses.fields(espn.EspnGame)

    assert [f.name for f in fields] == [
        "event_id",
        "league",
        "kickoff",
        "home",
        "away",
        "status",
        "winner",
        "spread_home",
        "spread_source",
        "neutral_site",
        "home_moneyline",
        "away_moneyline",
    ]
    defaults = {f.name: f.default for f in fields if f.default is not dataclasses.MISSING}
    assert defaults == {
        "spread_home": None,
        "spread_source": None,
        "neutral_site": False,
        "home_moneyline": None,
        "away_moneyline": None,
    }
    # Frozen, so a caller cannot quietly rewrite a spread after selection.
    assert espn.EspnGame.__dataclass_params__.frozen is True


def test_team_side_field_names_and_order_are_stable():
    fields = dataclasses.fields(espn.TeamSide)

    assert [f.name for f in fields] == ["name", "abbr", "canonical", "record", "score"]
    defaults = {f.name: f.default for f in fields if f.default is not dataclasses.MISSING}
    assert defaults == {"record": None, "score": None}
    assert espn.TeamSide.__dataclass_params__.frozen is True


def test_calendar_week_field_names_and_order_are_stable():
    fields = dataclasses.fields(espn.CalendarWeek)

    assert [f.name for f in fields] == ["season_type", "week", "label", "start", "end"]
    assert all(f.default is dataclasses.MISSING for f in fields)
    assert espn.CalendarWeek.__dataclass_params__.frozen is True


def test_is_final_is_a_property_not_a_stored_field():
    assert isinstance(espn.EspnGame.is_final, property)
    assert "is_final" not in {f.name for f in dataclasses.fields(espn.EspnGame)}


def test_fetch_scoreboard_signature_and_defaults():
    """results.py calls fetch_scoreboard(db, league, year, week, ttl_minutes=5), so
    week has to stay the fourth positional parameter."""
    assert _params(espn.fetch_scoreboard) == [
        ("db", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("league", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("year", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("week", "POSITIONAL_OR_KEYWORD", None),
        ("season_type", "POSITIONAL_OR_KEYWORD", espn.SEASON_TYPE_REGULAR),
        ("ttl_minutes", "POSITIONAL_OR_KEYWORD", 10),
    ]


def test_remaining_public_signatures_are_stable():
    assert _params(espn.fetch_core_odds) == [
        ("db", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("league", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("event_id", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
    ]
    assert _params(espn.detect_current_week) == [
        ("db", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("league", "POSITIONAL_OR_KEYWORD", "nfl"),
        ("year", "POSITIONAL_OR_KEYWORD", None),
        ("now", "POSITIONAL_OR_KEYWORD", None),
    ]
    assert _params(espn.parse_core_odds) == [
        ("payload", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("home_abbr", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("away_abbr", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
    ]
    assert _params(espn.parse_scoreboard) == [
        ("payload", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("league", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
    ]
    assert _params(espn.parse_event) == [
        ("event", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("league", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
    ]
    assert _params(espn.current_week_from_payload) == [
        ("payload", "POSITIONAL_OR_KEYWORD", "REQUIRED"),
        ("now", "POSITIONAL_OR_KEYWORD", None),
        ("season_type", "POSITIONAL_OR_KEYWORD", espn.SEASON_TYPE_REGULAR),
    ]


def test_season_type_constants():
    assert espn.SEASON_TYPE_REGULAR == 2
    assert espn.SEASON_TYPE_POSTSEASON == 3


# League paths and credit discipline -----------------------------------------


def test_league_paths_cover_nfl_and_fbs_only():
    assert set(espn.LEAGUE_PATHS) == {"nfl", "ncaaf"}
    assert espn.LEAGUE_PATHS["nfl"] == ("nfl", {})
    # groups=80 is the FBS filter. Without it the college scoreboard returns every
    # division and the slate fills with games nobody in the pool has heard of.
    assert espn.LEAGUE_PATHS["ncaaf"] == ("college-football", {"groups": "80"})


@pytest.mark.parametrize("league", ["nba", "", "NFL ", None])
def test_unknown_league_is_rejected(league):
    with pytest.raises(ValueError):
        espn._path(league)


def test_fetch_scoreboard_is_served_from_cache_and_spends_no_credit(db, load_fixture):
    """Offline mode is on for the whole session, so this can only come from the cache.
    ESPN is unmetered, so nothing may be written to the provider usage ledger."""
    payload = load_fixture(NFL_W5)
    cache_put(db, "espn:scoreboard:nfl:2025:2:5", payload)

    served = espn.fetch_scoreboard(db, "nfl", 2025, week=5)

    assert len(espn.parse_scoreboard(served, "nfl")) == 14
    assert db.query(ProviderUsage).count() == 0


def test_fetch_core_odds_is_served_from_cache_and_spends_no_credit(db, load_fixture):
    entry = load_fixture(NFL_CORE_ODDS)[SF_AT_LAR]
    cache_put(db, f"espn:coreodds:nfl:{SF_AT_LAR}", entry)

    served = espn.fetch_core_odds(db, "nfl", SF_AT_LAR)

    assert served["items"][0]["details"] == "LAR -8.5"
    assert db.query(ProviderUsage).count() == 0


def test_offline_fetch_without_a_cache_raises_instead_of_calling_out(db):
    with pytest.raises(ProviderError):
        espn.fetch_scoreboard(db, "nfl", 2025, week=9)

    assert db.query(ProviderUsage).count() == 0


def test_detect_current_week_degrades_to_none_when_nothing_is_cached(db):
    assert espn.detect_current_week(db, "nfl", 2026) is None


def test_detect_current_week_reads_the_cached_calendar(db, load_fixture):
    cache_put(db, "espn:calendar:nfl:2026", load_fixture(NFL_UPCOMING))

    week = espn.detect_current_week(
        db, "nfl", 2026, now=dt.datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    )

    assert week is not None
    assert week.week == 2
    assert db.query(ProviderUsage).count() == 0


# Credit discipline, the exact fetch_json keywords ----------------------------
#
# Counting ProviderUsage rows is not enough on its own. Offline mode short circuits
# fetch_json before the metered branch is ever reached, so a call that wrongly passed
# provider="odds_api" would still leave the ledger empty. These tests replace
# espn.fetch_json and read the keywords directly, which is the only way to prove ESPN
# stays unmetered and that a ttl is always set.


def _capture_fetch_json(monkeypatch, payload: Any) -> list[dict]:
    calls: list[dict] = []

    def fake_fetch_json(db, **kwargs):
        calls.append(kwargs)
        return payload, "cache"

    monkeypatch.setattr(espn, "fetch_json", fake_fetch_json)
    return calls


def _assert_unmetered(call: dict) -> None:
    """ESPN is keyless and unmetered, so it must never name a provider or buy credits."""
    assert call.get("provider") is None
    assert "credits" not in call
    assert call["ttl_minutes"] >= 1


def test_fetch_scoreboard_calls_fetch_json_unmetered_with_a_ten_minute_ttl(
    monkeypatch, load_fixture
):
    calls = _capture_fetch_json(monkeypatch, load_fixture(NFL_W5))

    espn.fetch_scoreboard(None, "nfl", 2025, week=5)

    assert len(calls) == 1
    call = calls[0]
    _assert_unmetered(call)
    assert call["cache_key"] == "espn:scoreboard:nfl:2025:2:5"
    assert call["url"] == f"{espn.SITE_BASE}/nfl/scoreboard"
    assert call["params"] == {"seasontype": 2, "dates": 2025, "week": 5}
    assert call["ttl_minutes"] == 10


def test_fetch_scoreboard_for_college_always_sends_the_fbs_group(monkeypatch, load_fixture):
    calls = _capture_fetch_json(monkeypatch, load_fixture(CFB_W5))

    espn.fetch_scoreboard(None, "ncaaf", 2025, week=5, season_type=3, ttl_minutes=5)

    call = calls[0]
    _assert_unmetered(call)
    assert call["cache_key"] == "espn:scoreboard:ncaaf:2025:3:5"
    assert call["url"] == f"{espn.SITE_BASE}/college-football/scoreboard"
    assert call["params"] == {"seasontype": 3, "dates": 2025, "groups": "80", "week": 5}
    assert call["ttl_minutes"] == 5


def test_fetch_scoreboard_without_a_week_keeps_a_distinct_cache_key(monkeypatch, load_fixture):
    calls = _capture_fetch_json(monkeypatch, load_fixture(NFL_W5))

    espn.fetch_scoreboard(None, "nfl", 2025)

    assert calls[0]["cache_key"] == "espn:scoreboard:nfl:2025:2:None"
    assert "week" not in calls[0]["params"]


def test_fetch_core_odds_calls_fetch_json_unmetered_and_caches_for_a_month(
    monkeypatch, load_fixture
):
    """A finished game's line never changes, so this must not re-request per page view."""
    calls = _capture_fetch_json(monkeypatch, load_fixture(NFL_CORE_ODDS)[SF_AT_LAR])

    espn.fetch_core_odds(None, "nfl", SF_AT_LAR)

    assert len(calls) == 1
    call = calls[0]
    _assert_unmetered(call)
    assert call["cache_key"] == f"espn:coreodds:nfl:{SF_AT_LAR}"
    expected_url = f"{espn.CORE_BASE}/nfl/events/{SF_AT_LAR}/competitions/{SF_AT_LAR}/odds"
    assert call["url"] == expected_url
    assert call["ttl_minutes"] == 60 * 24 * 30
    # The core odds URL takes no query string, so nothing can widen it into a loop.
    assert call.get("params") is None


def test_detect_current_week_calls_fetch_json_unmetered_with_an_hour_ttl(monkeypatch, load_fixture):
    calls = _capture_fetch_json(monkeypatch, load_fixture(NFL_UPCOMING))

    espn.detect_current_week(None, "nfl", 2026, now=dt.datetime(2026, 9, 20, tzinfo=UTC))

    assert len(calls) == 1
    call = calls[0]
    _assert_unmetered(call)
    assert call["cache_key"] == "espn:calendar:nfl:2026"
    assert call["url"] == f"{espn.SITE_BASE}/nfl/scoreboard"
    assert call["params"] == {"dates": 2026}
    assert call["ttl_minutes"] == 60


def test_detect_current_week_without_a_year_uses_the_current_cache_key(monkeypatch, load_fixture):
    calls = _capture_fetch_json(monkeypatch, load_fixture(NFL_UPCOMING))

    espn.detect_current_week(None, "ncaaf")

    assert calls[0]["cache_key"] == "espn:calendar:ncaaf:current"
    assert calls[0]["params"] == {"groups": "80"}


def test_espn_has_exactly_three_fetch_json_call_sites():
    """All three are pinned above. A fourth would be an unaudited network path."""
    source = pathlib.Path(espn.__file__).read_text(encoding="utf-8")

    assert source.count("fetch_json(") == 3


def test_espn_never_opens_a_socket_of_its_own():
    """Everything has to go through the governor in providers/http.py.

    Read from the import table rather than the raw text, so a comment mentioning a
    library cannot fail this and a real import cannot slip past it.
    """
    tree = ast.parse(pathlib.Path(espn.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported.isdisjoint({"httpx", "urllib", "requests", "socket", "aiohttp", "http"})
    assert "app" in imported  # it does reach the governor


# Repeat calls must not re-request ---------------------------------------------


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _CountingClient:
    """Stands in for httpx.Client and counts every GET, so a re-spend cannot hide."""

    calls: list[tuple[str, Any]] = []
    payload: Any = None

    def __init__(self, **kwargs) -> None:
        pass

    def __enter__(self) -> _CountingClient:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get(self, url: str, params: Any = None) -> _FakeResponse:
        _CountingClient.calls.append((url, params))
        return _FakeResponse(_CountingClient.payload)


def test_repeat_fetch_scoreboard_inside_the_ttl_makes_exactly_one_request(
    db, load_fixture, monkeypatch
):
    """The ttl is what keeps a busy game day from hammering a feed.

    Offline mode is lifted here only after httpx.Client has been replaced, and
    monkeypatch puts both back at teardown, so no socket exists for this to reach.
    """
    _CountingClient.calls = []
    _CountingClient.payload = load_fixture(NFL_W5)
    monkeypatch.setattr(http_mod.httpx, "Client", _CountingClient)
    assert http_mod.httpx.Client is _CountingClient
    monkeypatch.setattr(settings, "offline_mode", False)

    first = espn.fetch_scoreboard(db, "nfl", 2025, week=5)
    second = espn.fetch_scoreboard(db, "nfl", 2025, week=5)
    third = espn.fetch_scoreboard(db, "nfl", 2025, week=5)

    assert len(_CountingClient.calls) == 1
    url, params = _CountingClient.calls[0]
    assert url == f"{espn.SITE_BASE}/nfl/scoreboard"
    assert params == {"seasontype": 2, "dates": 2025, "week": 5}
    # Calls two and three came out of FeedCache, so they are equal but not the same
    # object as the live payload, which round trips through the JSON column.
    assert first == second == third
    assert len(espn.parse_scoreboard(third, "nfl")) == 14
    # ESPN is unmetered, so even a live request writes nothing to the ledger.
    assert db.query(ProviderUsage).count() == 0


def test_repeat_fetch_core_odds_inside_the_ttl_makes_exactly_one_request(
    db, load_fixture, monkeypatch
):
    _CountingClient.calls = []
    _CountingClient.payload = load_fixture(NFL_CORE_ODDS)[SF_AT_LAR]
    monkeypatch.setattr(http_mod.httpx, "Client", _CountingClient)
    assert http_mod.httpx.Client is _CountingClient
    monkeypatch.setattr(settings, "offline_mode", False)

    for _ in range(5):
        served = espn.fetch_core_odds(db, "nfl", SF_AT_LAR)

    assert len(_CountingClient.calls) == 1
    assert served["items"][0]["details"] == "LAR -8.5"
    assert db.query(ProviderUsage).count() == 0


def test_offline_mode_is_restored_after_the_live_path_tests():
    """Guards the two tests above. If monkeypatch ever stopped restoring this, the rest
    of the suite would silently gain permission to call out."""
    assert settings.offline_mode is True
