"""Router level tests for the commissioner facing payout summary page (Payout system rebuild,
Phase 6, app/routers/payouts.py's summary/player-paid-toggle/csv routes and
app/templates/admin/payout_summary.html).

Follows tests/test_payout_routes.py's own client/world/session_factory/_login fixture pattern
(Phase 4's closest sibling), a throwaway in-memory SQLite database wired into a real TestClient
via app.dependency_overrides, rather than the lower level `db` fixture tests/test_payout_service.py
uses: these tests exercise real HTTP requests against the real router and the real templates.

PayoutAward rows are inserted directly against the database in the `world` fixture rather than
driven through snapshot_awards or a full scored week: this page only ever reads frozen award
rows (app.services.payouts.payout_summary), never a live projection, so a direct row insert is
the simplest, most honest way to get real data in front of it, exactly the "simplest" option
the phase brief itself calls out over the heavier score_week_for_pool path.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, PayoutAward, Pool, PoolMember, User, utcnow
from app.templating import fmt_money

UTC = dt.UTC


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


def _make_pool(db: Session, **overrides) -> Pool:
    defaults = {
        "name": "Payout Summary Test Pool",
        "join_code": "SUMCODE1",
        "season_year": 2025,
        "num_games_per_week": 4,
        "target_nfl": 2,
        "target_ncaaf": 2,
        "sports": ["nfl", "ncaaf"],
        "timezone": "America/New_York",
        "current_week": 1,
    }
    defaults.update(overrides)
    pool = Pool(**defaults)
    db.add(pool)
    db.flush()
    return pool


def _make_user(db: Session, email: str, name: str, role: str = "player") -> User:
    user = User(
        email=email,
        password_hash=hash_password("hunter2hunter2"),
        display_name=name,
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def _award(
    db: Session,
    pool: Pool,
    user: User,
    scope: str,
    amount: Decimal,
    *,
    week_id: int | None = None,
    paid: bool = False,
) -> PayoutAward:
    award = PayoutAward(
        pool_id=pool.id,
        user_id=user.id,
        scope=scope,
        week_id=week_id,
        place=1,
        tied_with=1,
        amount=amount,
        pot_at_award=amount,
        rule_mode="amount",
        rule_value=amount,
        awarded_at=utcnow(),
        paid_at=utcnow() if paid else None,
    )
    db.add(award)
    db.flush()
    return award


@pytest.fixture
def world(session_factory):
    """A pool with a commissioner and two players. Alice has one award in each of the four
    scopes, none paid yet (100 + 50 + 600 + 325 = 1075 owed). Bob has a single, already paid
    weekly award (55, paid). The commissioner has no awards at all. This gives every test a
    settled player, an unsettled player, and an owed-nothing player in one fixture, and "Bob,
    Jr." exercises the CSV export's comma-in-a-display-name escaping."""
    db = session_factory()
    pool = _make_pool(db)
    boss = _make_user(db, "boss@example.com", "The Commissioner", role="admin")
    alice = _make_user(db, "alice@example.com", "Alice Anderson")
    bob = _make_user(db, "bob@example.com", "Bob, Jr.")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=alice.id, role_in_pool="member"))
    db.add(PoolMember(pool_id=pool.id, user_id=bob.id, role_in_pool="member"))
    db.flush()

    _award(db, pool, alice, "weekly", Decimal("100.00"))
    _award(db, pool, alice, "bowl", Decimal("50.00"))
    _award(db, pool, alice, "season_points", Decimal("600.00"))
    _award(db, pool, alice, "season_wins", Decimal("325.00"))
    _award(db, pool, bob, "weekly", Decimal("55.00"), paid=True)

    db.commit()
    data = {
        "pool_id": pool.id,
        "boss_id": boss.id,
        "alice_id": alice.id,
        "bob_id": bob.id,
    }
    db.close()
    return data


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/picks"}
    )
    assert response.status_code == 303, response.text


# Access control ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/admin/payouts/summary", None),
        ("post", "/admin/payouts/player/1/paid", {}),
        ("post", "/admin/payouts/award/1/paid", {}),
        ("get", "/admin/payouts/summary.csv", None),
    ],
)
def test_new_payout_routes_refused_for_a_regular_player(client, world, method, path, data):
    _login(client, "alice@example.com")
    response = (
        getattr(client, method)(path, data=data)
        if data is not None
        else getattr(client, method)(path)
    )
    assert response.status_code == 403


# The summary page reconciles ------------------------------------------------------------


def test_summary_reconciles_with_raw_award_amounts(client, world, session_factory):
    _login(client, "boss@example.com")

    db = session_factory()
    raw_total = sum(
        (
            a.amount
            for a in db.scalars(select(PayoutAward).where(PayoutAward.pool_id == world["pool_id"]))
        ),
        Decimal("0"),
    )
    db.close()
    assert raw_total == Decimal("1130.00")  # 100 + 50 + 600 + 325 + 55

    response = client.get("/admin/payouts/summary")
    assert response.status_code == 200
    expected = fmt_money(raw_total)
    # The running settlement line and the table's own totals row must both show the same,
    # correct grand total: neither is a re-derivation, both are sums over the exact same
    # PlayerPayoutRow fields the per-player rows themselves render.
    assert response.text.count(expected) >= 2
    assert f"Paid {fmt_money(Decimal('55'))} dollars of {expected} dollars." in response.text
    assert (
        "1 of 2 players settled." in response.text
    )  # bob settled; alice is not; boss owes nothing


# The paid toggle round-trips -------------------------------------------------------------


def test_player_paid_toggle_round_trips(client, world, session_factory):
    _login(client, "boss@example.com")

    response = client.post(f"/admin/payouts/player/{world['alice_id']}/paid", data={})
    assert response.status_code == 200  # an HTMX partial swap, never a redirect

    db = session_factory()
    alice_awards = list(
        db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == world["pool_id"], PayoutAward.user_id == world["alice_id"]
            )
        )
    )
    assert len(alice_awards) == 4
    assert all(a.paid_at is not None for a in alice_awards)
    db.close()

    response = client.post(f"/admin/payouts/player/{world['alice_id']}/paid", data={})
    assert response.status_code == 200

    db = session_factory()
    alice_awards = list(
        db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == world["pool_id"], PayoutAward.user_id == world["alice_id"]
            )
        )
    )
    assert all(a.paid_at is None for a in alice_awards)
    db.close()


# The CSV export ----------------------------------------------------------------------------


def test_csv_export_parses_with_expected_header_and_rows(client, world):
    _login(client, "boss@example.com")
    response = client.get("/admin/payouts/summary.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="payout-summary.csv"'

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == [
        "Player",
        "Weekly",
        "Bowl",
        "Season Points",
        "Season Wins",
        "Grand Total",
        "Paid",
        "Unpaid",
    ]
    # One data row per pool member: the commissioner (no awards), Alice, and Bob.
    assert len(rows) == 1 + 3

    by_name = {row[0]: row for row in rows[1:]}
    alice_row = by_name["Alice Anderson"]
    assert alice_row[1:] == ["100.00", "50.00", "600.00", "325.00", "1075.00", "0", "1075.00"]

    bob_row = by_name["Bob, Jr."]  # the comma in the name is real data, csv.writer must escape it
    assert bob_row[1:] == ["55.00", "0", "0", "0", "55.00", "55.00", "0.00"]


# The unpaid filter ---------------------------------------------------------------------------


def test_unpaid_filter_shows_only_players_still_owed_money(client, world):
    _login(client, "boss@example.com")
    response = client.get("/admin/payouts/summary?unpaid=1")
    assert response.status_code == 200
    assert "Alice Anderson" in response.text  # unpaid_total > 0
    assert "Bob, Jr." not in response.text  # fully paid, unpaid_total == 0


# A player with awards in all four scopes totals correctly -------------------------------


def test_player_with_all_four_scopes_totals_correctly(client, world, session_factory):
    db = session_factory()
    alice_awards = {
        a.scope: a.amount
        for a in db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == world["pool_id"], PayoutAward.user_id == world["alice_id"]
            )
        )
    }
    db.close()
    assert set(alice_awards) == {"weekly", "bowl", "season_points", "season_wins"}
    expected_grand_total = sum(alice_awards.values(), Decimal("0"))
    assert expected_grand_total == Decimal("1075.00")

    _login(client, "boss@example.com")
    response = client.get("/admin/payouts/summary.csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    alice_row = next(row for row in rows if row[0] == "Alice Anderson")
    assert alice_row[1:5] == ["100.00", "50.00", "600.00", "325.00"]
    assert Decimal(alice_row[5]) == expected_grand_total
