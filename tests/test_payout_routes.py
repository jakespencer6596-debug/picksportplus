"""Router level tests for the commissioner facing Set Payouts screen (Payout system rebuild,
Phase 4, app/routers/payouts.py and app/templates/admin/payouts.html).

Follows tests/test_app.py's own client/world/session_factory fixture pattern (a throwaway in
memory SQLite database wired into a real TestClient via app.dependency_overrides, plus a
_login helper that posts real credentials through /login) rather than the lower level `db`
session fixture tests/test_payout_service.py uses: these tests exercise real HTTP requests
against the real router and the real template, not the service layer directly.

save_rule's own docstring (app/routers/payouts.py) documents the create-versus-update contract
this file's own tests assume: a POST to /admin/payouts/rule with no rule_id is always a
create (rejected if it collides with an existing (scope, place) pair), and a POST with an
existing rule_id is always an update of that exact row, never a second insert.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, PayoutRule, Pool, PoolMember, User
from app.payouts import Rule, resolve_rule

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
        "name": "Payout Test Pool",
        "join_code": "PAYCODE1",
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


@pytest.fixture
def world(session_factory):
    """A pool with a commissioner and a plain, non-commissioner player. No week or slate:
    the payouts screen does not need either."""
    db = session_factory()
    pool = _make_pool(db)
    boss = _make_user(db, "boss@example.com", "The Commissioner", role="admin")
    player = _make_user(db, "player@example.com", "Regular Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    db.commit()
    data = {"pool_id": pool.id, "boss_id": boss.id, "player_id": player.id}
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
        ("get", "/admin/payouts", None),
        ("post", "/admin/payouts/pot", {}),
        ("post", "/admin/payouts/rule", {}),
        ("post", "/admin/payouts/rule/1/delete", {}),
        ("post", "/admin/payouts/scale-to-pot", {}),
        ("post", "/admin/payouts/load-preset", {}),
    ],
)
def test_payout_routes_refused_for_a_regular_player(client, world, method, path, data):
    _login(client, "player@example.com")
    response = (
        getattr(client, method)(path, data=data)
        if data is not None
        else getattr(client, method)(path)
    )
    assert response.status_code == 403


# Create / update / delete one rule ---------------------------------------------------------


def test_create_rule_persists_to_db(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "amount", "value": "105", "label": "1st"},
    )
    assert response.status_code == 303

    db = session_factory()
    row = db.scalar(
        select(PayoutRule).where(
            PayoutRule.pool_id == world["pool_id"],
            PayoutRule.scope == "weekly",
            PayoutRule.place == 1,
        )
    )
    assert row is not None
    assert row.mode == "amount"
    assert row.value == Decimal("105.0000")
    assert row.label == "1st"
    db.close()


def test_update_rule_via_rule_id_replaces_the_row(client, world, session_factory):
    """save_rule's contract: a POST with rule_id set updates that exact row, in place, rather
    than erroring on the (pool_id, scope, place) unique constraint or inserting a second row."""
    _login(client, "boss@example.com")
    client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "amount", "value": "105"},
    )
    db = session_factory()
    row = db.scalar(
        select(PayoutRule).where(
            PayoutRule.pool_id == world["pool_id"], PayoutRule.scope == "weekly"
        )
    )
    rule_id = row.id
    db.close()

    response = client.post(
        "/admin/payouts/rule",
        data={
            "scope": "weekly",
            "place": "1",
            "mode": "percent",
            "value": "2.5",
            "rule_id": str(rule_id),
        },
    )
    assert response.status_code == 303

    db = session_factory()
    rows = list(
        db.scalars(
            select(PayoutRule).where(
                PayoutRule.pool_id == world["pool_id"], PayoutRule.scope == "weekly"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].id == rule_id
    assert rows[0].mode == "percent"
    assert rows[0].value == Decimal("2.5000")
    db.close()


def test_delete_rule_removes_it(client, world, session_factory):
    _login(client, "boss@example.com")
    client.post(
        "/admin/payouts/rule",
        data={"scope": "bowl", "place": "1", "mode": "amount", "value": "250"},
    )
    db = session_factory()
    rule_id = db.scalar(select(PayoutRule.id).where(PayoutRule.pool_id == world["pool_id"]))
    db.close()

    response = client.post(f"/admin/payouts/rule/{rule_id}/delete")
    assert response.status_code == 303

    db = session_factory()
    assert db.get(PayoutRule, rule_id) is None
    db.close()


def test_duplicate_scope_place_on_create_is_rejected(client, world, session_factory):
    _login(client, "boss@example.com")
    client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "amount", "value": "105"},
    )
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "amount", "value": "999"},
    )
    # A flash-and-redirect, never a 500 from a raw IntegrityError.
    assert response.status_code == 303

    db = session_factory()
    rows = list(
        db.scalars(
            select(PayoutRule).where(
                PayoutRule.pool_id == world["pool_id"], PayoutRule.scope == "weekly"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].value == Decimal("105.0000")  # the original, untouched
    db.close()


def test_negative_value_is_rejected(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "amount", "value": "-5"},
    )
    assert response.status_code == 303
    db = session_factory()
    assert db.scalar(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"])) is None
    db.close()


def test_percent_value_over_100_is_rejected(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "percent", "value": "150"},
    )
    assert response.status_code == 303
    db = session_factory()
    assert db.scalar(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"])) is None
    db.close()


def test_unknown_scope_is_rejected(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "not_a_scope", "place": "1", "mode": "amount", "value": "5"},
    )
    assert response.status_code == 303
    db = session_factory()
    assert db.scalar(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"])) is None
    db.close()


def test_non_integer_place_is_rejected_by_fastapi(client, world):
    """place is a typed int Form field: FastAPI's own coercion 422s before the route body
    (and therefore before this router's own explicit validation) ever runs."""
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "one", "mode": "amount", "value": "5"},
    )
    assert response.status_code == 422


def test_weekly_payout_weeks_out_of_range_is_rejected(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/admin/payouts/pot",
        data={
            "entry_fee": "",
            "pot_override": "",
            "weekly_payout_weeks": "31",
            "payout_rounding": "dollar",
            "payout_tiebreak": "earliest_submit",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.weekly_payout_weeks == 15  # unchanged, the model default
    db.close()


# Bulk actions -------------------------------------------------------------------------------


def test_load_preset_seeds_twelve_rules_across_four_scopes(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post("/admin/payouts/load-preset")
    assert response.status_code == 303

    db = session_factory()
    rows = list(db.scalars(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"])))
    assert len(rows) == 12
    by_scope: dict[str, int] = {}
    for row in rows:
        by_scope[row.scope] = by_scope.get(row.scope, 0) + 1
    assert by_scope == {"weekly": 3, "bowl": 3, "season_points": 3, "season_wins": 3}
    pool = db.get(Pool, world["pool_id"])
    assert pool.weekly_payout_weeks == 15
    db.close()


def test_summary_renders_known_ladder_totals(client, world, session_factory):
    _login(client, "boss@example.com")
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    # An override is the simplest way to land on the commissioner's own spreadsheet example
    # (a $4,950 pot) without needing 49 real paid members.
    pool.pot_override = Decimal("4950")
    db.commit()
    db.close()

    client.post("/admin/payouts/load-preset")
    response = client.get("/admin/payouts")
    assert response.status_code == 200
    # fmt_money drops the trailing .00 on a whole dollar figure (app/templating.py), so a
    # comma-formatted "2,775" would never actually appear; assert the bare digit run instead.
    for needle in ("2775", "400", "1155", "620", "4950"):
        assert needle in response.text, needle


def test_scale_to_pot_converts_every_rule_and_keeps_resolved_dollars_unchanged(
    client, world, session_factory
):
    _login(client, "boss@example.com")
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.pot_override = Decimal("4950")
    db.commit()
    db.close()

    client.post("/admin/payouts/load-preset")

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pot = pool.pot_override
    rows_before = list(db.scalars(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"])))
    before = {
        (row.scope, row.place): resolve_rule(
            Rule(
                scope=row.scope,
                place=row.place,
                mode=row.mode,
                value=Decimal(row.value),
                label=row.label,
            ),
            pot,
        )
        for row in rows_before
    }
    db.close()

    response = client.post("/admin/payouts/scale-to-pot")
    assert response.status_code == 303

    db = session_factory()
    rows_after = list(db.scalars(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"])))
    assert all(row.mode == "percent" for row in rows_after)
    for row in rows_after:
        rule = Rule(
            scope=row.scope,
            place=row.place,
            mode=row.mode,
            value=Decimal(row.value),
            label=row.label,
        )
        resolved_now = resolve_rule(rule, pot)
        original = before[(row.scope, row.place)]
        assert resolved_now.quantize(Decimal("0.01")) == original.quantize(Decimal("0.01"))
    db.close()


def test_scale_to_pot_with_no_pot_flashes_error_and_changes_nothing(client, world, session_factory):
    """entry_fee unset, no pot_override, nobody paid: the effective pot is Decimal("0"), so
    scaling must refuse rather than dividing by zero."""
    _login(client, "boss@example.com")
    client.post(
        "/admin/payouts/rule",
        data={"scope": "weekly", "place": "1", "mode": "amount", "value": "105"},
    )
    response = client.post("/admin/payouts/scale-to-pot")
    assert response.status_code == 303

    db = session_factory()
    row = db.scalar(select(PayoutRule).where(PayoutRule.pool_id == world["pool_id"]))
    assert row.mode == "amount"  # untouched
    assert row.value == Decimal("105.0000")
    db.close()


def test_over_allocated_pot_saves_successfully_and_shows_exceed_banner(
    client, world, session_factory
):
    _login(client, "boss@example.com")
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.pot_override = Decimal("10")  # far smaller than the standard ladder totals
    db.commit()
    db.close()

    response = client.post("/admin/payouts/load-preset")
    assert response.status_code == 303  # saving succeeds regardless of balance

    page = client.get("/admin/payouts")
    assert page.status_code == 200
    assert "exceed" in page.text.lower()


def test_pot_override_takes_precedence_over_computed_pot_in_summary(client, world, session_factory):
    _login(client, "boss@example.com")
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    boss = db.get(User, world["boss_id"])
    pool.entry_fee = Decimal("100")
    member = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == boss.id)
    )
    member.paid_at = dt.datetime.now(UTC)  # computed pot would be 100 * 1 = 100
    pool.pot_override = Decimal("25")  # the override the summary must actually use
    db.commit()
    db.close()

    # A one-time scope (no weekly_payout_weeks multiplier) so the math stays simple: 100% of
    # the pot resolves to exactly the pot, whichever pot is actually in force.
    client.post(
        "/admin/payouts/rule",
        data={"scope": "bowl", "place": "1", "mode": "percent", "value": "100"},
    )

    response = client.get("/admin/payouts")
    assert response.status_code == 200
    assert "Computed (not in use)" in response.text
    assert "Override (in use)" in response.text
    assert "$25" in response.text  # the effective pot and the bowl payout, both driven by it
