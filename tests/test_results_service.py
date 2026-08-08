"""Tests for app/services/results.py reading per-league resolved weeks (Phase 1 follow-up).

fetch_results had the same bug fetch_candidates did before Phase 1: it sent the pool's own
week_number straight to ESPN as the literal week number for every league, which is wrong
whenever NFL and college have drifted apart. It must instead read week.resolved_weeks (set by
app/services/ingest.py's fetch_candidates when the slate was built) and ask each league for its
own resolved week and season type, falling back to week_number only for an unresolved league.
"""

from __future__ import annotations

import datetime as dt

from app.models import Game, Pool, Week
from app.providers.http import cache_put
from app.services.results import fetch_results

UTC = dt.UTC


def _pool(db, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"RESCODE{id(overrides) % 100000}",
        "season_year": 2026,
        "sports": ["nfl", "ncaaf"],
        "timezone": "America/New_York",
        "current_week": 1,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _game(db, week: Week, *, league: str, event_id: str) -> Game:
    game = Game(
        week_id=week.id,
        league=league,
        espn_event_id=event_id,
        start_time=dt.datetime(2026, 9, 12, 17, 0, tzinfo=UTC),
        home_team="Home Team",
        away_team="Away Team",
        home_abbr="HOM",
        away_abbr="AWY",
        canonical_home_key=f"{league}-home",
        canonical_away_key=f"{league}-away",
        in_slate=True,
    )
    db.add(game)
    db.flush()
    return game


def test_fetch_results_uses_resolved_week_per_league_not_pool_week_number(db):
    """A pool week whose leagues resolved to different ESPN weeks (the whole point of Phase 1)
    must ask each league for ITS resolved week when refreshing scores, not the pool's own
    week_number, which is what results.py did before this fix.
    """
    pool = _pool(db)
    week = Week(
        pool_id=pool.id,
        season_year=2026,
        week_number=1,  # the pool's own sequence number: neither league's real ESPN week
        anchor_date=dt.date(2026, 9, 12),
        resolved_weeks={
            "nfl": {"week": 1, "season_type": 2},
            "ncaaf": {"week": 3, "season_type": 2},
        },
        label="Week 1",
        status="open",
    )
    db.add(week)
    db.flush()
    _game(db, week, league="nfl", event_id="nfl-1")
    _game(db, week, league="ncaaf", event_id="ncaaf-1")

    # Cache keyed on each league's OWN resolved week (1 for nfl, 3 for ncaaf), never on
    # week.week_number (1 for both), which is what proves the fix: a cache entry keyed on the
    # pool's week_number for ncaaf would simply not exist and the fetch would fail the test.
    cache_put(
        db,
        "espn:scoreboard:nfl:2026:2:1",
        {
            "events": [
                {
                    "id": "nfl-1",
                    "date": "2026-09-12T17:00Z",
                    "status": {"type": {"completed": True, "state": "post"}},
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "24",
                                    "winner": True,
                                    "team": {"abbreviation": "HOM", "displayName": "Home Team"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "17",
                                    "winner": False,
                                    "team": {"abbreviation": "AWY", "displayName": "Away Team"},
                                },
                            ]
                        }
                    ],
                }
            ]
        },
    )
    cache_put(
        db,
        "espn:scoreboard:ncaaf:2026:2:3",
        {
            "events": [
                {
                    "id": "ncaaf-1",
                    "date": "2026-09-12T17:00Z",
                    "status": {"type": {"completed": True, "state": "post"}},
                    "competitions": [
                        {
                            "competitors": [
                                {
                                    "homeAway": "home",
                                    "score": "10",
                                    "winner": False,
                                    "team": {"abbreviation": "HOM", "displayName": "Home Team"},
                                },
                                {
                                    "homeAway": "away",
                                    "score": "20",
                                    "winner": True,
                                    "team": {"abbreviation": "AWY", "displayName": "Away Team"},
                                },
                            ]
                        }
                    ],
                }
            ]
        },
    )

    report = fetch_results(db, pool, week)

    assert report.finals == 2
    assert report.warnings == []
    games = {g.espn_event_id: g for g in db.query(Game).all()}
    assert games["nfl-1"].winner == "home"
    assert games["ncaaf-1"].winner == "away"


def test_fetch_results_falls_back_to_week_number_when_a_league_never_resolved(db):
    """A league with no resolved_weeks entry (unanchored pool, or that league had no games for
    the window) falls back to the old behaviour: send week_number straight to ESPN.
    """
    pool = _pool(db)
    week = Week(
        pool_id=pool.id,
        season_year=2026,
        week_number=5,
        resolved_weeks={},
        label="Week 5",
        status="open",
    )
    db.add(week)
    db.flush()
    _game(db, week, league="nfl", event_id="nfl-5")

    cache_put(db, "espn:scoreboard:nfl:2026:2:5", {"events": []})

    report = fetch_results(db, pool, week)

    assert report.warnings == []
    assert report.pending == 1
