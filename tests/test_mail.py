"""Unit tests for app/services/mail.py's send() failure contract (Phase 7 remediation, see
DECISIONS.md).

Every test here monkeypatches app.services.mail._call_resend_api, the one place a real HTTP
call happens, so nothing in this file ever opens a socket, matching this codebase's offline
first testing rule (SPEC.md Section 17, tests/conftest.py's force_offline_mode).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import MailLog
from app.services import mail


def _logs(db, actor_key: str | None = None) -> list[MailLog]:
    query = select(MailLog)
    if actor_key is not None:
        query = query.where(MailLog.actor_key == actor_key)
    return list(db.scalars(query))


@pytest.fixture
def mail_enabled(monkeypatch):
    monkeypatch.setattr(settings, "mail_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "mail_from_address", "noreply@example.com")
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 20)


def test_send_returns_a_maillog_row_on_success(db, mail_enabled, monkeypatch):
    calls = []
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: calls.append(kwargs) or None)

    row = mail.send(
        db,
        to="Player@Example.com",
        subject="Hi",
        html="<p>hi</p>",
        text="hi",
        kind="test",
        actor_key="user:1",
    )

    assert isinstance(row, MailLog)
    assert row.result == "sent"
    assert row.recipient == "player@example.com"  # normalized
    assert len(calls) == 1
    assert calls[0]["to"] == "player@example.com"


def test_send_raises_mail_disabled_when_not_enabled(db, monkeypatch):
    monkeypatch.setattr(settings, "mail_enabled", False)

    with pytest.raises(mail.MailDisabled):
        mail.send(
            db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
        )

    row = _logs(db)[0]
    assert row.result == "disabled"


def test_send_raises_mail_disabled_when_enabled_but_unconfigured(db, monkeypatch):
    """mail_enabled alone is not enough: a missing key or from-address must also refuse,
    never attempt a call with a blank credential."""
    monkeypatch.setattr(settings, "mail_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "mail_from_address", "noreply@example.com")

    with pytest.raises(mail.MailDisabled):
        mail.send(
            db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
        )


def test_send_raises_mail_send_failed_and_logs_it(db, mail_enabled, monkeypatch):
    def _boom(**kwargs):
        raise mail.MailSendFailed("provider exploded")

    monkeypatch.setattr(mail, "_call_resend_api", _boom)

    with pytest.raises(mail.MailSendFailed):
        mail.send(
            db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
        )

    row = _logs(db)[0]
    assert row.result == "failed"
    assert "provider exploded" in row.detail


def test_send_rate_limits_after_the_configured_number_of_sends(db, mail_enabled, monkeypatch):
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 2)
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: None)

    for _ in range(2):
        mail.send(
            db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
        )

    with pytest.raises(mail.MailRateLimited):
        mail.send(
            db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
        )

    rows = _logs(db, "user:1")
    assert [r.result for r in rows] == ["sent", "sent", "rate_limited"]


def test_rate_limit_is_scoped_per_actor_key(db, mail_enabled, monkeypatch):
    """A different sender is never blocked by someone else's volume."""
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 1)
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: None)

    mail.send(
        db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
    )
    # user:1 is now at its limit, user:2 is untouched.
    row = mail.send(
        db, to="b@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:2"
    )
    assert row.result == "sent"


def test_rate_limit_only_counts_successful_sends_in_the_window(db, mail_enabled, monkeypatch):
    """A prior failed or disabled attempt does not eat into the sender's send budget."""
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 1)

    def _boom(**kwargs):
        raise mail.MailSendFailed("nope")

    monkeypatch.setattr(mail, "_call_resend_api", _boom)
    with pytest.raises(mail.MailSendFailed):
        mail.send(
            db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
        )

    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: None)
    row = mail.send(
        db, to="a@example.com", subject="s", html="h", text="t", kind="test", actor_key="user:1"
    )
    assert row.result == "sent"


def test_text_to_html_escapes_and_wraps_paragraphs():
    html = mail.text_to_html("Hello <b>world</b>.\n\nSecond paragraph.")
    assert "&lt;b&gt;" in html
    assert html.count("<p>") == 2
