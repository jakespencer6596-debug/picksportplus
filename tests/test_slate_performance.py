"""Page weight and form count budget for the slate editor (Phase 1, weekly tiebreak/sorting/
performance work, see PERF-REPORT.md). The one thing that stops this regressing the next time
someone adds a control to a row: a rendered 20 game, 100 candidate slate must stay under 150KB
and 60 <form> elements. Before this phase the same scenario measured 518,028 bytes and 405
forms (see PERF-REPORT.md's Phase 0 baseline), almost entirely from a swap <select> repeated
in full inside every one of the 20 on-slate rows and every candidate rendered at once.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, Game, Pool, PoolMember, User, Week

UTC = dt.UTC

MAX_BYTES = 150_000
MAX_FORMS = 60
MAX_OPTIONS = 200


@pytest.fixture
def big_slate_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db

    db = session_factory()
    pool = Pool(
        name="Big Slate Pool",
        join_code="BIGSLATE",
        season_year=2025,
        num_games_per_week=20,
        target_nfl=8,
        target_ncaaf=12,
        picks_required=15,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        timezone="America/New_York",
        current_week=5,
        payment_required_to_pick=False,
    )
    db.add(pool)
    db.flush()
    boss = User(
        email="boss@example.com",
        password_hash=hash_password("hunter2hunter2"),
        display_name="The Commissioner",
        role="admin",
    )
    db.add(boss)
    db.flush()
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))

    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=5,
        label="Week 5",
        status="open",
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=48),
    )
    db.add(week)
    db.flush()

    base_time = dt.datetime.now(UTC) + dt.timedelta(hours=48)

    def make_game(i: int, *, in_slate: bool, league: str) -> Game:
        return Game(
            week_id=week.id,
            league=league,
            espn_event_id=f"evt{i}",
            start_time=base_time + dt.timedelta(hours=i % 72),
            home_team=f"Home Team {i}",
            away_team=f"Away Team {i}",
            home_abbr=f"H{i}",
            away_abbr=f"A{i}",
            canonical_home_key=f"{league}:home-{i}",
            canonical_away_key=f"{league}:away-{i}",
            spread_home=-1.0 - (i % 14) * 0.5,
            closeness=(i % 14) * 0.5,
            spread_source="espn",
            in_slate=in_slate,
            slate_rank=(i + 1) if in_slate else None,
            status="scheduled",
        )

    for i in range(20):
        db.add(make_game(i, in_slate=True, league="nfl" if i < 8 else "ncaaf"))
    for i in range(20, 120):
        db.add(make_game(i, in_slate=False, league="nfl" if i % 2 == 0 else "ncaaf"))
    db.commit()
    db.close()

    with TestClient(app, follow_redirects=False) as client:
        response = client.post(
            "/login",
            data={"email": "boss@example.com", "password": "hunter2hunter2", "next": "/picks"},
        )
        assert response.status_code == 303, response.text
        client.week_number = week.week_number
        yield client

    app.dependency_overrides.clear()


def test_slate_editor_page_weight_budget(big_slate_client: TestClient):
    """20 games on the slate, 100 candidates, no picks yet (the heaviest, fully editable
    state): the rendered page must stay under 150KB, 60 forms and 200 <option> elements,
    regardless of how many candidates exist."""
    response = big_slate_client.get(f"/league/slate?week={big_slate_client.week_number}")
    assert response.status_code == 200
    body = response.text

    assert (
        len(response.content) < MAX_BYTES
    ), f"Slate editor rendered {len(response.content)} bytes, over the {MAX_BYTES} budget."

    form_count = len(re.findall(r"<form[ >]", body, re.IGNORECASE))
    assert (
        form_count <= MAX_FORMS
    ), f"Slate editor rendered {form_count} <form> elements, over the {MAX_FORMS} budget."

    option_count = len(re.findall(r"<option[ >]", body, re.IGNORECASE))
    assert (
        option_count < MAX_OPTIONS
    ), f"Slate editor rendered {option_count} <option> elements, over the {MAX_OPTIONS} budget."

    # The swap list itself must still cover every real candidate exactly once, not per row:
    # one <datalist id="swap-candidates"> holding all 100 off-slate games. The remaining
    # options are the two mobile "Sort by" <select> controls (Phase 4), a small, fixed count
    # that does not grow with the candidate count, unlike the datalist.
    assert body.count('id="swap-candidates"') == 1
    datalist_start = body.index('id="swap-candidates"')
    datalist_end = body.index("</datalist>", datalist_start)
    datalist_html = body[datalist_start:datalist_end]
    assert len(re.findall(r"<option[ >]", datalist_html, re.IGNORECASE)) == 100


def test_pin_action_is_a_small_htmx_partial_not_the_whole_page(big_slate_client: TestClient):
    """The whole point: pinning one game must not re-send the 500KB+ page. An HTMX request
    gets back only the affected row plus the header summary and error slot, comfortably under
    5KB, versus the roughly 150KB a full page reload still costs for this same scenario."""
    response = big_slate_client.post(
        "/league/slate/game",
        data={"week_id": "1", "game_id": "1", "action": "pin"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert len(response.content) < 10_000
    assert 'id="slate-row-1"' in response.text
    assert 'id="week-summary-body"' in response.text
    # Pin never touches which table a game belongs to, so it must not carry the two full
    # table refreshes a membership-changing action (add/remove/swap) needs.
    assert 'id="on-slate-tbody"' not in response.text
    assert 'id="candidates-tbody"' not in response.text
