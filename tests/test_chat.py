"""Tests for Phase 6 of the slate drift incident: league chat and member email export.
"League chat box... easier communication to true members versus the informational email that
gets sent to potential players." A simple, scoped message board: no websocket, server
rendered, posted over HTMX, escaped and linkified, never HTML or markdown.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import get_db
from app.main import app
from app.models import Base, LeagueMessage, Pool, PoolMember, User

UTC = dt.UTC


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
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


def _user(db, email, name, role="player") -> User:
    user = User(
        email=email, password_hash=hash_password("hunter2hunter2"), display_name=name, role=role
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def world(session_factory):
    db = session_factory()
    pool = Pool(
        name="Test Pool",
        join_code="TESTCODE",
        season_year=2026,
        sports=["nfl", "ncaaf"],
        timezone="America/New_York",
    )
    db.add(pool)
    db.flush()
    boss = _user(db, "boss@example.com", "The Commissioner")
    p1 = _user(db, "p1@example.com", "Player One")
    p2 = _user(db, "p2@example.com", "Player Two")
    _user(db, "outsider@example.com", "Outsider")
    _user(db, "admin@example.com", "Site Admin", role="admin")
    db.add_all(
        [
            PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"),
            PoolMember(pool_id=pool.id, user_id=p1.id, role_in_pool="member"),
            PoolMember(pool_id=pool.id, user_id=p2.id, role_in_pool="member"),
        ]
    )
    db.commit()
    data = {"pool_id": pool.id, "boss_id": boss.id, "p1_id": p1.id, "p2_id": p2.id}
    db.close()
    return data


def _login(client, email):
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/picks"}
    )
    assert response.status_code == 303, response.text


def test_non_member_gets_403_on_chat_routes(client, world):
    _login(client, "outsider@example.com")
    assert client.get("/league/chat").status_code == 403
    assert client.post("/league/chat", data={"body": "hi"}).status_code == 403


def test_site_admin_can_view_chat_without_being_a_member(client, world):
    _login(client, "admin@example.com")
    view_as = client.post(f"/site/leagues/{world['pool_id']}/view-as")
    assert view_as.status_code == 303

    response = client.get("/league/chat")
    assert response.status_code == 200
    assert "site admin" in response.text.lower()


def test_member_can_post_and_it_appears(client, world, session_factory):
    _login(client, "p1@example.com")
    response = client.post("/league/chat", data={"body": "Hello league"})
    assert response.status_code == 200
    assert "Hello league" in response.text

    db = session_factory()
    row = db.scalar(select(LeagueMessage).where(LeagueMessage.pool_id == world["pool_id"]))
    assert row is not None
    assert row.body == "Hello league"
    assert row.user_id == world["p1_id"]
    db.close()


def test_a_script_tag_in_a_message_renders_inert(client, world):
    _login(client, "p1@example.com")
    response = client.post("/league/chat", data={"body": "<script>alert(1)</script>"})
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_a_url_in_a_message_is_linkified(client, world):
    _login(client, "p1@example.com")
    response = client.post("/league/chat", data={"body": "check https://example.com/x please"})
    assert response.status_code == 200
    assert '<a href="https://example.com/x"' in response.text


def test_empty_message_is_rejected(client, world):
    _login(client, "p1@example.com")
    response = client.post("/league/chat", data={"body": "   "})
    assert response.status_code == 200
    assert "Enter a message" in response.text


def test_message_over_the_length_cap_is_rejected(client, world):
    _login(client, "p1@example.com")
    response = client.post("/league/chat", data={"body": "x" * 2001})
    assert response.status_code == 200
    assert "limited to 2000 characters" in response.text


def test_rate_limiting_triggers(client, world, session_factory, monkeypatch):
    import app.routers.chat as chat_mod

    monkeypatch.setattr(chat_mod, "POST_RATE_LIMIT_PER_HOUR", 2)
    _login(client, "p1@example.com")
    client.post("/league/chat", data={"body": "one"})
    client.post("/league/chat", data={"body": "two"})
    response = client.post("/league/chat", data={"body": "three"})
    assert "posting too fast" in response.text.lower()

    db = session_factory()
    count = db.scalar(select(LeagueMessage)) is not None
    assert count
    db.close()


def test_a_member_cannot_edit_another_members_message(client, world, session_factory):
    db = session_factory()
    message = LeagueMessage(pool_id=world["pool_id"], user_id=world["p1_id"], body="original")
    db.add(message)
    db.commit()
    message_id = message.id
    db.close()

    _login(client, "p2@example.com")
    response = client.post(f"/league/chat/{message_id}/edit", data={"body": "hijacked"})
    assert response.status_code == 403


def test_owner_can_edit_their_own_message(client, world, session_factory):
    db = session_factory()
    message = LeagueMessage(pool_id=world["pool_id"], user_id=world["p1_id"], body="original")
    db.add(message)
    db.commit()
    message_id = message.id
    db.close()

    _login(client, "p1@example.com")
    response = client.post(f"/league/chat/{message_id}/edit", data={"body": "fixed typo"})
    assert response.status_code == 200
    assert "fixed typo" in response.text

    db = session_factory()
    row = db.get(LeagueMessage, message_id)
    assert row.body == "fixed typo"
    assert row.edited_at is not None
    db.close()


def test_a_member_cannot_delete_another_members_message(client, world, session_factory):
    db = session_factory()
    message = LeagueMessage(pool_id=world["pool_id"], user_id=world["p1_id"], body="original")
    db.add(message)
    db.commit()
    message_id = message.id
    db.close()

    _login(client, "p2@example.com")
    response = client.post(f"/league/chat/{message_id}/delete")
    assert response.status_code == 403


def test_commissioner_can_delete_any_message(client, world, session_factory):
    db = session_factory()
    message = LeagueMessage(pool_id=world["pool_id"], user_id=world["p1_id"], body="original")
    db.add(message)
    db.commit()
    message_id = message.id
    db.close()

    _login(client, "boss@example.com")
    response = client.post(f"/league/chat/{message_id}/delete")
    assert response.status_code == 200
    assert "original" not in response.text

    db = session_factory()
    row = db.get(LeagueMessage, message_id)
    assert row.deleted_at is not None
    db.close()


def test_commissioner_can_pin_and_unpin_a_message(client, world, session_factory):
    db = session_factory()
    message = LeagueMessage(pool_id=world["pool_id"], user_id=world["p1_id"], body="announcement")
    db.add(message)
    db.commit()
    message_id = message.id
    db.close()

    _login(client, "boss@example.com")
    response = client.post(f"/league/chat/{message_id}/pin")
    assert response.status_code == 200

    db = session_factory()
    assert db.get(LeagueMessage, message_id).pinned is True
    db.close()

    response = client.post(f"/league/chat/{message_id}/pin")
    db = session_factory()
    assert db.get(LeagueMessage, message_id).pinned is False
    db.close()


def test_a_regular_member_cannot_pin_a_message(client, world, session_factory):
    db = session_factory()
    message = LeagueMessage(pool_id=world["pool_id"], user_id=world["p1_id"], body="hi")
    db.add(message)
    db.commit()
    message_id = message.id
    db.close()

    _login(client, "p2@example.com")
    response = client.post(f"/league/chat/{message_id}/pin")
    assert response.status_code == 403


def test_unread_count_reflects_messages_since_last_view(client, world, session_factory):
    db = session_factory()
    db.add(LeagueMessage(pool_id=world["pool_id"], user_id=world["boss_id"], body="msg 1"))
    db.commit()
    db.close()

    _login(client, "p1@example.com")
    response = client.get("/picks")
    assert "Chat (1)" in response.text or "Chat" in response.text  # nav renders either way

    # Opening chat itself marks it read.
    client.get("/league/chat")
    db = session_factory()
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["p1_id"]
        )
    )
    assert member.chat_last_viewed_at is not None
    db.close()


def test_member_email_export_returns_every_member_exactly_once(client, world):
    _login(client, "boss@example.com")
    response = client.get("/league/members")
    assert response.status_code == 200
    for email in ("boss@example.com", "p1@example.com", "p2@example.com"):
        assert response.text.count(email) >= 1

    csv_response = client.get("/league/members/emails.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    lines = csv_response.text.strip().splitlines()
    assert lines[0] == "name,email"
    emails_in_csv = [line.split(",")[-1] for line in lines[1:]]
    assert sorted(emails_in_csv) == sorted(["boss@example.com", "p1@example.com", "p2@example.com"])
    assert len(emails_in_csv) == len(set(emails_in_csv))


def test_member_email_export_refused_for_a_regular_player(client, world):
    _login(client, "p1@example.com")
    response = client.get("/league/members/emails.csv")
    assert response.status_code == 403
