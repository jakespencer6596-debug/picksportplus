"""Tests for app/services/calendar.py: per-league week resolution from a calendar date.

NFL and college football week numbers are not aligned (Spec Section 5a), so a pool's anchor
date has to be resolved against each league's own calendar independently. These tests run
against two recorded-shaped calendar fixtures:

  espn_nfl_upcoming_with_odds.json   the real 2026 NFL calendar, already used by
                                      test_providers.py for espn.current_week_from_payload.
  espn_cfb_2026_calendar.json        the 2026 college calendar, built by shifting the real
                                      recorded 2025 college calendar (espn_cfb_2025_w5.json)
                                      forward by exactly 364 days (52 weeks), which preserves
                                      the weekday alignment and keeps every week window
                                      historically plausible without fabricating a live
                                      capture that was never taken.

Saturday September 12, 2026 is the concrete real world case this module exists for: the
2026 launch date is NFL week 1 and college week 3.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.models import Pool, ProviderUsage
from app.providers import espn
from app.providers.http import cache_put
from app.services import calendar

UTC = dt.UTC

NFL_2026 = "espn_nfl_upcoming_with_odds.json"
CFB_2026 = "espn_cfb_2026_calendar.json"


def _cache_calendar(db, load_fixture, league: str, year: int, filename: str) -> None:
    cache_put(db, f"espn:scoreboard:{league}:{year}:2:None", load_fixture(filename))


def _pool(**overrides: Any) -> Pool:
    defaults = {"season_year": 2026, "sports": ["nfl", "ncaaf"]}
    defaults.update(overrides)
    return Pool(**defaults)


# resolve_league_week ----------------------------------------------------------


def test_launch_date_resolves_nfl_to_week_one(db, load_fixture):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026)

    resolution = calendar.resolve_league_week(db, "nfl", 2026, dt.date(2026, 9, 12))

    assert resolution is not None
    assert resolution.league == "nfl"
    assert resolution.week == 1
    assert resolution.season_type == espn.SEASON_TYPE_REGULAR
    assert resolution.is_postseason is False
    assert resolution.start <= calendar.anchor_datetime(dt.date(2026, 9, 12)) < resolution.end


def test_launch_date_resolves_college_to_week_three(db, load_fixture):
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)

    resolution = calendar.resolve_league_week(db, "ncaaf", 2026, dt.date(2026, 9, 12))

    assert resolution is not None
    assert resolution.league == "ncaaf"
    assert resolution.week == 3
    assert resolution.season_type == espn.SEASON_TYPE_REGULAR


def test_resolve_pool_weeks_resolves_both_leagues_for_the_launch_date(db, load_fixture):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)
    pool = _pool()

    resolved = calendar.resolve_pool_weeks(db, pool, dt.date(2026, 9, 12))

    assert resolved["nfl"].week == 1
    assert resolved["ncaaf"].week == 3
    assert db.query(ProviderUsage).count() == 0  # ESPN stays unmetered


def test_resolve_pool_weeks_only_covers_the_pools_enabled_leagues(db, load_fixture):
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)
    pool = _pool(sports=["ncaaf"])

    resolved = calendar.resolve_pool_weeks(db, pool, dt.date(2026, 9, 12))

    assert set(resolved) == {"ncaaf"}


# The college season has not started yet, NFL has ----------------------------


def test_college_already_underway_resolves_to_week_one(db, load_fixture):
    """August 29, 2026: college week 1 is running, NFL has not started."""
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)

    resolution = calendar.resolve_league_week(db, "ncaaf", 2026, dt.date(2026, 8, 29))

    assert resolution is not None
    assert resolution.week == 1


def test_nfl_before_its_season_resolves_to_none_not_an_exception(db, load_fixture):
    """The same August 29, 2026 date is outside both NFL's regular season and its
    postseason, so it must resolve to None rather than raising or guessing."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026)

    resolution = calendar.resolve_league_week(db, "nfl", 2026, dt.date(2026, 8, 29))

    assert resolution is None


def test_resolve_pool_weeks_lets_one_league_resolve_and_the_other_go_none(db, load_fixture):
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)
    pool = _pool()

    resolved = calendar.resolve_pool_weeks(db, pool, dt.date(2026, 8, 29))

    assert resolved["nfl"] is None
    assert resolved["ncaaf"] is not None
    assert resolved["ncaaf"].week == 1


# Bowl season ------------------------------------------------------------------


def test_december_anchor_resolves_college_to_the_postseason(db, load_fixture):
    """December 19, 2026 is after college's regular season ends but inside bowl season."""
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)

    resolution = calendar.resolve_league_week(db, "ncaaf", 2026, dt.date(2026, 12, 19))

    assert resolution is not None
    assert resolution.season_type == espn.SEASON_TYPE_POSTSEASON
    assert resolution.is_postseason is True
    assert resolution.label == "Bowls"


def test_december_anchor_leaves_nfl_in_its_regular_season(db, load_fixture):
    """The same December date is still an ordinary NFL regular season week."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026)

    resolution = calendar.resolve_league_week(db, "nfl", 2026, dt.date(2026, 12, 19))

    assert resolution is not None
    assert resolution.season_type == espn.SEASON_TYPE_REGULAR
    assert resolution.is_postseason is False


# Genuinely outside both windows for every league -------------------------------


def test_anchor_after_every_calendar_resolves_to_none_for_both_leagues(db, load_fixture):
    """March 2027 is after both leagues' postseasons end and before either fixture's next
    season starts. Neither league may raise, both resolve to None."""
    _cache_calendar(db, load_fixture, "nfl", 2026, NFL_2026)
    _cache_calendar(db, load_fixture, "ncaaf", 2026, CFB_2026)
    pool = _pool()

    resolved = calendar.resolve_pool_weeks(db, pool, dt.date(2027, 3, 6))

    assert resolved == {"nfl": None, "ncaaf": None}


# Never raises on a provider outage --------------------------------------------


def test_resolve_league_week_degrades_to_none_when_nothing_is_cached(db):
    """Offline mode is on for the whole test session (see conftest.py), so with nothing
    cached this can only fail closed, never raise."""
    resolution = calendar.resolve_league_week(db, "nfl", 2026, dt.date(2026, 9, 12))
    assert resolution is None


# Determinism and caching -------------------------------------------------------


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _CountingClient:
    """Stands in for httpx.Client. Counts every GET, so a re-spend cannot hide."""

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


def test_repeated_resolution_makes_exactly_one_http_call_per_league(db, load_fixture, monkeypatch):
    """Resolving the same (league, year) for several different anchor dates must not
    re-request. Offline mode is lifted only after httpx.Client has been replaced, and
    monkeypatch restores both at teardown."""
    from app.config import settings
    from app.providers import http as http_mod

    _CountingClient.calls = []
    _CountingClient.payload = load_fixture(NFL_2026)
    monkeypatch.setattr(http_mod.httpx, "Client", _CountingClient)
    monkeypatch.setattr(settings, "offline_mode", False)

    anchors = [dt.date(2026, 9, 12), dt.date(2026, 10, 3), dt.date(2026, 12, 19)]
    results = [calendar.resolve_league_week(db, "nfl", 2026, anchor) for anchor in anchors]

    assert len(_CountingClient.calls) == 1
    assert all(r is not None for r in results)
    assert [r.week for r in results] == [1, 4, 15]
    assert db.query(ProviderUsage).count() == 0


def test_repeated_resolve_pool_weeks_makes_one_call_per_league(db, load_fixture, monkeypatch):
    from app.config import settings
    from app.providers import http as http_mod

    nfl_payload = load_fixture(NFL_2026)
    cfb_payload = load_fixture(CFB_2026)

    def _fake_client(**kwargs):
        return _RoutingClient()

    class _RoutingClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url: str, params: Any = None) -> _FakeResponse:
            _RoutingClient.calls.append(url)
            payload = nfl_payload if "/nfl/" in url else cfb_payload
            return _FakeResponse(payload)

    _RoutingClient.calls = []
    monkeypatch.setattr(http_mod.httpx, "Client", _fake_client)
    monkeypatch.setattr(settings, "offline_mode", False)

    pool = _pool()
    for anchor in (dt.date(2026, 9, 12), dt.date(2026, 9, 19), dt.date(2026, 12, 19)):
        calendar.resolve_pool_weeks(db, pool, anchor)

    assert len(_RoutingClient.calls) == 2  # exactly one per league, ever
    assert sum(1 for u in _RoutingClient.calls if "/nfl/" in u) == 1
    assert sum(1 for u in _RoutingClient.calls if "/college-football/" in u) == 1


def test_offline_mode_is_restored_after_the_live_path_tests():
    from app.config import settings

    assert settings.offline_mode is True
