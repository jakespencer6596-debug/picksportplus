"""Transactional email: one send() entry point over a single provider.

Provider: Resend (https://resend.com), called directly over its plain REST API with httpx
(already a dependency, no SDK added). See DECISIONS.md, Phase 7, for the full justification
(why Resend over a competitor, and why not raw SMTP).

The failure contract is the whole point of this module (the problem this phase exists to fix
is a caller silently believing an email went out when nothing was sent). send() returns a
MailLog row ONLY on a real, successful send. Every other outcome, mail turned off or not
configured, this sender over its rate limit, or the provider call itself failing, raises one
of MailDisabled / MailRateLimited / MailSendFailed (all MailError) instead of returning a
value a caller could mistake for success. There is no boolean return to forget to check: a
caller either gets a MailLog row back, or an exception it must handle.

Every call, whatever the outcome, is logged to MailLog before send() returns or raises, via
db.add()+db.flush() on the caller's own session (the same flush-now-commit-later shape every
other write in this codebase already follows: callers commit at the end of their own request,
exactly as they already do for every other change made in the same handler). This deliberately
does NOT go through app/providers/http.py's cached fetch_json: a send is not cacheable or
idempotent the way a schedule fetch is, and mixing it into that machinery would risk a retry
double-sending an email that already reached its recipient.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import logging

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import normalize_email
from app.config import settings
from app.models import MailLog, utcnow

log = logging.getLogger("picksportplus.mail")

RESEND_API_URL = "https://api.resend.com/emails"

# A send is a single, small request, never retried the way a feed pull is (a retried send
# could double-deliver an email, which a retried GET never risks): a short, fixed timeout of
# its own rather than reusing settings.http_timeout_seconds/http_retries, which exist for
# app/providers/http.py's cached, idempotent feed reads, a different problem.
MAIL_TIMEOUT_SECONDS = 10.0


class MailError(RuntimeError):
    """Base for every send() failure. A caller must catch this (or a subclass) to treat a
    send as anything other than successful."""


class MailDisabled(MailError):
    """MAIL_ENABLED is off, or the provider key/from-address is not configured."""


class MailRateLimited(MailError):
    """This actor already sent settings.mail_rate_limit_per_hour emails in the last hour."""


class MailSendFailed(MailError):
    """The provider call itself failed: bad key, network error, non-2xx response."""


def text_to_html(text: str) -> str:
    """A minimal, safe HTML body from a plain text message: escaped, paragraphs on a blank
    line, single line breaks kept within a paragraph. No template engine, no styling, matching
    the plainspoken, sentence case voice every other email in this app uses."""
    escaped = html_lib.escape(text.strip())
    paragraphs = [p for p in escaped.split("\n\n") if p]
    body = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return f"<!doctype html><html><body>{body}</body></html>"


def _log(
    db: Session, *, to: str, kind: str, result: str, actor_key: str, detail: str | None = None
) -> MailLog:
    row = MailLog(recipient=to, kind=kind, result=result, actor_key=actor_key, detail=detail)
    db.add(row)
    db.flush()
    return row


def _rate_limited(db: Session, actor_key: str) -> bool:
    cutoff = utcnow() - dt.timedelta(hours=1)
    count = (
        db.scalar(
            select(func.count())
            .select_from(MailLog)
            .where(
                MailLog.actor_key == actor_key,
                MailLog.result == "sent",
                MailLog.created_at >= cutoff,
            )
        )
        or 0
    )
    return count >= settings.mail_rate_limit_per_hour


def _call_resend_api(*, to: str, subject: str, html: str, text: str) -> None:
    """The one place an outbound send actually happens, kept tiny and separate so a test can
    monkeypatch this single function and never open a real socket, matching this codebase's
    offline-first testing rule (SPEC.md Section 17, tests/conftest.py's force_offline_mode)."""
    from_header = settings.mail_from_address
    if settings.mail_from_name:
        from_header = f"{settings.mail_from_name} <{settings.mail_from_address}>"
    try:
        response = httpx.post(
            RESEND_API_URL,
            timeout=MAIL_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={"from": from_header, "to": [to], "subject": subject, "html": html, "text": text},
        )
    except httpx.HTTPError as exc:
        raise MailSendFailed(f"Could not reach the mail provider: {exc}") from exc
    if response.status_code >= 300:
        raise MailSendFailed(f"Mail provider returned HTTP {response.status_code}.")


def send(
    db: Session,
    *,
    to: str,
    subject: str,
    html: str,
    text: str,
    kind: str,
    actor_key: str,
) -> MailLog:
    """Send one email and log the attempt no matter what happens.

    kind names the template/purpose (commissioner_invite, player_invite, password_reset,
    week_published, test), stored on MailLog for the /site/mail panel. actor_key is the rate
    limit bucket, "user:{id}" for a signed in sender or "email:{address}" for the one
    anonymous case, a password reset request.

    Returns the MailLog row on a real, successful send. Raises MailDisabled, MailRateLimited
    or MailSendFailed for every other outcome; see the module docstring for why this is a
    raise, not a status field a caller could ignore.
    """
    address = normalize_email(to)

    if not settings.mail_enabled or not settings.resend_api_key or not settings.mail_from_address:
        _log(
            db,
            to=address,
            kind=kind,
            result="disabled",
            actor_key=actor_key,
            detail="Mail is turned off or not configured for this deployment.",
        )
        raise MailDisabled(
            "Email is not turned on for this deployment. Share the link or code directly instead."
        )

    if _rate_limited(db, actor_key):
        _log(
            db,
            to=address,
            kind=kind,
            result="rate_limited",
            actor_key=actor_key,
            detail=f"More than {settings.mail_rate_limit_per_hour} sends in the last hour.",
        )
        raise MailRateLimited(
            f"Too many emails sent in the last hour (limit {settings.mail_rate_limit_per_hour}). "
            "Wait a while, or share the link directly."
        )

    try:
        _call_resend_api(to=address, subject=subject, html=html, text=text)
    except MailSendFailed as exc:
        _log(db, to=address, kind=kind, result="failed", actor_key=actor_key, detail=str(exc)[:500])
        log.warning("mail send failed, kind=%s to=%s: %s", kind, address, exc)
        raise

    return _log(db, to=address, kind=kind, result="sent", actor_key=actor_key)


__all__ = [
    "MailError",
    "MailDisabled",
    "MailRateLimited",
    "MailSendFailed",
    "send",
    "text_to_html",
]
