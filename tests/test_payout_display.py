"""Player facing display tests for the payout system rebuild, Phase 5: the payout column on
Weekly Results (app/routers/results.py, app/templates/results.html) and the two season award
panels on Season Standings (app/routers/leaderboard.py, app/templates/leaderboard.html).

Follows tests/test_app.py's own client/world/session_factory/_login pattern (a throwaway in
memory SQLite database wired into a real TestClient via app.dependency_overrides) rather than
the lower level `db` fixture tests/test_payout_service.py uses, matching tests/test_payout_
routes.py's own precedent for router-level payout tests: these exercise real HTTP requests
against the real router and the real template, not the service layer directly.

Standings are built directly as WeekEntry rows rather than driven through real picks
submissions and score_week_for_pool: that machinery is exercised elsewhere (test_payout_
service.py, test_scoring.py), and building WeekEntry rows by hand is what lets each test set
up an exact, deterministic ranking without wrestling a real slate into the right outcome.
Freezing an award is done the same way, a direct call to app.services.payouts.snapshot_awards,
exactly as score_week_for_pool itself calls it once a week finishes scoring.
"""

from __future__ import annotations

import itertools
import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, PayoutRule, Pool, PoolMember, User, Week, WeekEntry
from app.services import payouts as payout_service

_join_codes = itertools.count()


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def client(session_factory):
    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


def _pool(db: Session, **overrides) -> Pool:
    defaults = {
        "name": "Test Pool",
        "join_code": f"PAYDISP{next(_join_codes)}",
        "season_year": 2025,
        "sports": ["nfl", "ncaaf"],
        "timezone": "America/New_York",
        "current_week": 1,
        "scoring_mode": "standard",
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _user(db: Session, email: str, name: str, role: str = "player") -> User:
    user = User(
        email=email, password_hash=hash_password("hunter2hunter2"), display_name=name, role=role
    )
    db.add(user)
    db.flush()
    return user


def _member(db: Session, pool: Pool, user: User, role: str = "member") -> PoolMember:
    member = PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool=role)
    db.add(member)
    db.flush()
    return member


def _week(
    db: Session,
    pool: Pool,
    *,
    week_number: int = 5,
    status: str = "open",
    is_bowl_week: bool = False,
) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=week_number,
        label=f"Week {week_number}",
        status=status,
        is_bowl_week=is_bowl_week,
    )
    db.add(week)
    db.flush()
    return week


def _entry(
    db: Session,
    pool: Pool,
    week: Week,
    user: User,
    *,
    points: int = 0,
    is_winner: bool = False,
) -> WeekEntry:
    entry = WeekEntry(
        user_id=user.id,
        pool_id=pool.id,
        week_id=week.id,
        points=points,
        correct=0,
        possible=0,
        is_winner=is_winner,
    )
    db.add(entry)
    db.flush()
    return entry


def _rule(db: Session, pool: Pool, scope: str, place: int, value: str) -> PayoutRule:
    rule = PayoutRule(
        pool_id=pool.id, scope=scope, place=place, mode="amount", value=Decimal(value)
    )
    db.add(rule)
    db.flush()
    return rule


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/picks"}
    )
    assert response.status_code == 303, response.text


# A naive `"0 dollars" not in response.text` substring check false-positives on "100 dollars"
# (which literally contains the substring "0 dollars"), so this matches only a genuine
# standalone zero, guarded by a word boundary on both sides.
_ZERO_DOLLARS = re.compile(r"\b0(\.00)? dollars\b")


def _assert_no_blank_payout_rendered_as_zero(text: str) -> None:
    match = _ZERO_DOLLARS.search(text)
    assert match is None, f"Found a literal zero-dollar payout: {match.group(0)!r}"


# Results: a final (scored) week's payout column ------------------------------------------


def _build_scored_week_with_rules(session_factory):
    """Alice (40 pts) beats Bob (20 pts) beats Carol (5 pts). Weekly rules only pay 1st and
    2nd, so Carol ranks but is not paid: exactly the "rules exist, this player just did not
    place" case the payout column has to render blank, not $0, for.
    """
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner", role="admin")
    alice = _user(db, "alice@example.com", "Alice Alpha")
    bob = _user(db, "bob@example.com", "Bob Beta")
    carol = _user(db, "carol@example.com", "Carol Gamma")
    _member(db, pool, boss, role="commissioner")
    _member(db, pool, alice)
    _member(db, pool, bob)
    _member(db, pool, carol)
    week = _week(db, pool, status="open")
    _entry(db, pool, week, alice, points=40)
    _entry(db, pool, week, bob, points=20)
    _entry(db, pool, week, carol, points=5)
    _rule(db, pool, "weekly", 1, "100")
    _rule(db, pool, "weekly", 2, "40")
    db.commit()
    ids = {"pool_id": pool.id, "week_id": week.id, "boss_email": "boss@example.com"}
    db.close()
    return ids


def test_final_week_payout_column_shows_frozen_amounts_no_projected_label(client, session_factory):
    ids = _build_scored_week_with_rules(session_factory)

    db = session_factory()
    pool = db.get(Pool, ids["pool_id"])
    week = db.get(Week, ids["week_id"])
    week.status = "scored"
    payout_service.snapshot_awards(db, pool, "weekly", week=week)
    db.commit()
    db.close()

    _login(client, ids["boss_email"])
    response = client.get("/results")
    assert response.status_code == 200
    assert "Payout" in response.text
    assert "Projected" not in response.text
    assert "100 dollars" in response.text  # Alice, 1st
    assert "40 dollars" in response.text  # Bob, 2nd
    # Carol placed but is not paid (only two places are configured): blank, never "0 dollars".
    _assert_no_blank_payout_rendered_as_zero(response.text)


def test_player_with_no_award_for_the_week_renders_blank_not_zero(client, session_factory):
    """Carol ranks (3rd) but no configured rule pays 3rd place, so her own payout cell must
    render blank, not "0 dollars". Scoped to Carol's own <tr>, not the whole page, so this
    cannot be fooled by another player's real, nonzero amount elsewhere on the table.
    """
    ids = _build_scored_week_with_rules(session_factory)

    db = session_factory()
    pool = db.get(Pool, ids["pool_id"])
    week = db.get(Week, ids["week_id"])
    week.status = "scored"
    payout_service.snapshot_awards(db, pool, "weekly", week=week)
    db.commit()
    db.close()

    _login(client, ids["boss_email"])
    response = client.get("/results")
    assert response.status_code == 200

    # Isolate Carol's own <tr>...</tr>, not the nearest one anywhere in the document: a naive
    # non-greedy regex anchored at the first "<tr" in the page would span every row between
    # the table's start and Carol's name (including Alice's and Bob's real, nonzero amounts),
    # producing a false failure. Finding her name first, then the nearest enclosing tag pair
    # around just that name, is what actually scopes the check to her own row.
    name_index = response.text.index("Carol Gamma")
    row_start = response.text.rindex("<tr", 0, name_index)
    row_end = response.text.index("</tr>", name_index) + len("</tr>")
    row_html = response.text[row_start:row_end]
    assert "dollars" not in row_html


def test_locked_not_yet_scored_week_shows_projected_live_amount(client, session_factory):
    ids = _build_scored_week_with_rules(session_factory)

    db = session_factory()
    week = db.get(Week, ids["week_id"])
    week.status = "locked"  # games still playing out, never snapshotted
    db.commit()
    db.close()

    _login(client, ids["boss_email"])
    response = client.get("/results")
    assert response.status_code == 200
    assert "Payout" in response.text
    assert "Projected" in response.text
    # The live projection resolves to the exact same amount-mode figures, since these rules
    # do not depend on the pot at all.
    assert "100 dollars" in response.text
    assert "40 dollars" in response.text
    _assert_no_blank_payout_rendered_as_zero(response.text)


def test_no_payout_rules_for_the_applicable_scope_hides_the_column(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner", role="admin")
    player = _user(db, "player@example.com", "Regular Player")
    _member(db, pool, boss, role="commissioner")
    _member(db, pool, player)
    week = _week(db, pool, status="scored")
    _entry(db, pool, week, player, points=10)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Payout" not in response.text


def test_tied_week_splits_the_amount_and_it_sums_to_the_allocated_total(client, session_factory):
    """Alice and Bob tie for 1st (both 15 points): they split the combined 1st (90) and 2nd
    (30) place amounts, 120 total, evenly, 60 each. Carol, alone in 3rd, gets the 3rd place
    rule (10) untouched by the split.
    """
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner", role="admin")
    alice = _user(db, "alice@example.com", "Alice Alpha")
    bob = _user(db, "bob@example.com", "Bob Beta")
    carol = _user(db, "carol@example.com", "Carol Gamma")
    _member(db, pool, boss, role="commissioner")
    _member(db, pool, alice)
    _member(db, pool, bob)
    _member(db, pool, carol)
    week = _week(db, pool, status="open")
    _entry(db, pool, week, alice, points=15)
    _entry(db, pool, week, bob, points=15)
    _entry(db, pool, week, carol, points=5)
    _rule(db, pool, "weekly", 1, "90")
    _rule(db, pool, "weekly", 2, "30")
    _rule(db, pool, "weekly", 3, "10")
    week.status = "scored"
    payout_service.snapshot_awards(db, pool, "weekly", week=week)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    # Two tied players each show the same split amount, and it visibly sums to the 120 the
    # tied group was allocated (90 + 30, the 1st and 2nd place amounts they share).
    assert response.text.count("60 dollars") == 2
    assert response.text.count("10 dollars") == 1


# Season standings: the two award panels ----------------------------------------------------


def test_season_awards_panels_absent_with_no_frozen_season_awards(client, session_factory):
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner", role="admin")
    _member(db, pool, boss, role="commissioner")
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/standings")
    assert response.status_code == 200
    assert "Season: Points" not in response.text
    assert "Season: Wins" not in response.text


def test_season_awards_panels_show_once_frozen_and_a_player_can_appear_in_both(
    client, session_factory
):
    """Alice leads the season in points (rank 1, awarded) but is only 2nd in weekly wins
    (still awarded, a different amount): she has to appear in both panels, each with her own
    correct figure. Bob leads in weekly wins but trails Alice on points. Carol trails both
    scopes and is unplaced (only two places are configured in either scope), so she must
    appear in neither panel.
    """
    db = session_factory()
    pool = _pool(db)
    boss = _user(db, "boss@example.com", "The Commissioner", role="admin")
    alice = _user(db, "alice@example.com", "Alice Alpha")
    bob = _user(db, "bob@example.com", "Bob Beta")
    carol = _user(db, "carol@example.com", "Carol Gamma")
    _member(db, pool, boss, role="commissioner")
    _member(db, pool, alice)
    _member(db, pool, bob)
    _member(db, pool, carol)

    # Three regular weeks' worth of WeekEntry rows: Alice totals the most points (40), Bob
    # the most weekly wins (3), Carol trails on both.
    weeks = [_week(db, pool, week_number=n, status="scored") for n in (1, 2, 3)]
    _entry(db, pool, weeks[0], alice, points=10, is_winner=False)
    _entry(db, pool, weeks[1], alice, points=15, is_winner=False)
    _entry(db, pool, weeks[2], alice, points=15, is_winner=True)
    _entry(db, pool, weeks[0], bob, points=10, is_winner=True)
    _entry(db, pool, weeks[1], bob, points=10, is_winner=True)
    _entry(db, pool, weeks[2], bob, points=10, is_winner=True)
    _entry(db, pool, weeks[0], carol, points=10, is_winner=False)
    _entry(db, pool, weeks[1], carol, points=5, is_winner=False)
    _entry(db, pool, weeks[2], carol, points=5, is_winner=False)
    # Alice: 40 points, 1 win. Bob: 30 points, 3 wins. Carol: 20 points, 0 wins.

    _rule(db, pool, "season_points", 1, "600")
    _rule(db, pool, "season_points", 2, "300")
    _rule(db, pool, "season_wins", 1, "333")
    _rule(db, pool, "season_wins", 2, "111")
    payout_service.snapshot_awards(db, pool, "season_points", week=None)
    payout_service.snapshot_awards(db, pool, "season_wins", week=None)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/standings")
    assert response.status_code == 200
    assert "Season: Points" in response.text
    assert "Season: Wins" in response.text
    # Alice: 1st in points (600), 2nd in wins (111). Bob: 2nd in points (300), 1st in wins
    # (333). Both appear in both panels, each with their own correct amount.
    assert "600 dollars" in response.text
    assert "300 dollars" in response.text
    assert "333 dollars" in response.text
    assert "111 dollars" in response.text
    # Carol placed in neither scope (rank 3 in both, and only two places are configured in
    # either), so exactly four award rows should render on the whole page, not five.
    assert response.text.count(" dollars") == 4
