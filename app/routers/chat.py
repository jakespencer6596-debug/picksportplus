"""League chat (Phase 6, slate drift incident): "a way to collate all emails or create a
league chat box... easier communication to true members versus the informational email that
gets sent to potential players." A simple, scoped message board, not real-time chat: no
websocket, polled on a modest interval (30s, see chat.html), server rendered, posted over HTMX
so the page never reloads.
"""

from __future__ import annotations

import datetime as dt
import html

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_active_pool, is_commissioner, membership_for, require_user
from app.db import get_db
from app.models import LeagueMessage, Pool, PoolMember, User, utcnow
from app.templating import render, templates

router = APIRouter(prefix="/league/chat", tags=["chat"])

# A message longer than this is refused outright (Phase 6: "Cap message length at 2000
# characters"), matched on the server, never trusted from a client side maxlength alone.
MAX_MESSAGE_LENGTH = 2000

# How many messages one member may post in an hour before being asked to slow down. There is
# no product requirement for an exact number, only that some limit exists; this is generous
# enough that no real conversation ever hits it by accident, and cheap enough to compute (one
# count query) that it needs no separate rate-limit table the way app.services.mail's own
# MailLog-backed limiter has, since a league chat message carries no delivery cost to throttle.
POST_RATE_LIMIT_PER_HOUR = 30


def _require_member(db: Session, user: User, pool: Pool) -> PoolMember | None:
    """None for the site admin viewing a league they are not a member of (still allowed in,
    clearly labelled); raises 403 for anyone else who is not a real member of this pool."""
    member = membership_for(db, user, pool)
    if member is None and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Members of this league only.")
    return member


def unread_chat_count(db: Session, pool: Pool, member: PoolMember | None) -> int:
    """How many non-deleted messages exist after this member's own last-viewed timestamp.
    None member (the site admin browsing a league they never joined) always reads as 0: there
    is no per-viewer state to track for someone with no PoolMember row at all."""
    if member is None:
        return 0
    query = select(func.count(LeagueMessage.id)).where(
        LeagueMessage.pool_id == pool.id, LeagueMessage.deleted_at.is_(None)
    )
    if member.chat_last_viewed_at is not None:
        query = query.where(LeagueMessage.created_at > member.chat_last_viewed_at)
    return int(db.scalar(query) or 0)


def _visible_messages(db: Session, pool: Pool) -> list[LeagueMessage]:
    """Pinned first, then newest last within each group, so the page reads top to bottom as a
    normal conversation with any pinned announcement fixed at the very top."""
    rows = list(
        db.scalars(
            select(LeagueMessage)
            .where(LeagueMessage.pool_id == pool.id, LeagueMessage.deleted_at.is_(None))
            .order_by(LeagueMessage.created_at.asc())
        )
    )
    pinned = [r for r in rows if r.pinned]
    rest = [r for r in rows if not r.pinned]
    return pinned + rest


def _messages_fragment(request: Request, db: Session, pool: Pool, user: User) -> str:
    frag = templates.env.get_template("chat/_messages.html").module
    return str(
        frag.messages_list(
            _visible_messages(db, pool), user, is_commissioner(db, user, pool), pool.timezone
        )
    )


@router.get("")
def chat_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    member = _require_member(db, user, pool)
    # Opening the page marks it read: the whole point of the unread count is "messages posted
    # since I last opened this," so opening it resets that clock. A None member (site admin
    # with no real row) has nothing to update.
    if member is not None:
        member.chat_last_viewed_at = utcnow()
        db.commit()

    return render(
        request,
        "chat/chat.html",
        {
            "messages": _visible_messages(db, pool),
            "is_member": member is not None,
            "viewing_as_admin": member is None and user.is_admin,
            "max_length": MAX_MESSAGE_LENGTH,
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="chat",
    )


@router.post("")
def chat_post(
    request: Request,
    body: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    _require_member(db, user, pool)
    body = body.strip()
    error: str | None = None
    if not body:
        error = "Enter a message."
    elif len(body) > MAX_MESSAGE_LENGTH:
        error = f"Messages are limited to {MAX_MESSAGE_LENGTH} characters."
    else:
        since = utcnow() - dt.timedelta(hours=1)
        recent = (
            db.scalar(
                select(func.count(LeagueMessage.id)).where(
                    LeagueMessage.pool_id == pool.id,
                    LeagueMessage.user_id == user.id,
                    LeagueMessage.created_at >= since,
                )
            )
            or 0
        )
        if recent >= POST_RATE_LIMIT_PER_HOUR:
            error = "You are posting too fast. Wait a bit and try again."

    if error is None:
        db.add(LeagueMessage(pool_id=pool.id, user_id=user.id, body=body))
        db.commit()

    html_body = _messages_fragment(request, db, pool, user)
    if error:
        html_body += f'<div class="flash flash-error" id="chat-error">{html.escape(error)}</div>'
    else:
        html_body += '<div id="chat-error"></div>'
    return HTMLResponse(html_body)


def _own_or_commissioner_message(
    db: Session, user: User, pool: Pool, message_id: int
) -> LeagueMessage:
    message = db.get(LeagueMessage, message_id)
    if message is None or message.pool_id != pool.id or message.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That message no longer exists.")
    return message


@router.post("/{message_id}/edit")
def chat_edit(
    request: Request,
    message_id: int,
    body: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    _require_member(db, user, pool)
    message = _own_or_commissioner_message(db, user, pool, message_id)
    if message.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only edit your own message.")
    body = body.strip()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A message cannot be empty.")
    if len(body) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Messages are limited to {MAX_MESSAGE_LENGTH} characters."
        )
    message.body = body
    message.edited_at = utcnow()
    db.commit()
    return HTMLResponse(_messages_fragment(request, db, pool, user))


@router.post("/{message_id}/delete")
def chat_delete(
    request: Request,
    message_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    _require_member(db, user, pool)
    message = _own_or_commissioner_message(db, user, pool, message_id)
    if message.user_id != user.id and not is_commissioner(db, user, pool):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete your own message.")
    message.deleted_at = utcnow()
    db.commit()
    return HTMLResponse(_messages_fragment(request, db, pool, user))


@router.post("/{message_id}/pin")
def chat_pin(
    request: Request,
    message_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """Commissioner only, either direction: pin an announcement to the top, or unpin one that
    no longer needs to sit there. Toggling never affects any other message's own pinned state."""
    if not is_commissioner(db, user, pool):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Commissioner access only.")
    message = _own_or_commissioner_message(db, user, pool, message_id)
    message.pinned = not message.pinned
    db.commit()
    return HTMLResponse(_messages_fragment(request, db, pool, user))


@router.get("/messages")
def chat_messages(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """The HTMX poll target (chat.html: hx-trigger="every 30s"). Deliberately does not update
    chat_last_viewed_at: a background poll while the tab merely sits open is not the same
    signal as the member actually opening the page, which is what GET /league/chat itself
    already marks read."""
    _require_member(db, user, pool)
    return HTMLResponse(_messages_fragment(request, db, pool, user))


__all__ = ["router", "unread_chat_count"]
