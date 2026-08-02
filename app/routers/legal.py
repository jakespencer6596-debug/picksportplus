"""Terms of service and privacy policy.

Both pages are deliberately public. Someone deciding whether to create an account has to be
able to read them first, so there is no require_user here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, is_commissioner, membership_for
from app.config import settings
from app.db import get_db
from app.models import User
from app.templating import render

router = APIRouter(tags=["legal"])


def _chrome(db: Session, user: User | None) -> dict:
    """Show the signed-in navigation when there is a session, and the plain bar otherwise."""
    if user is None:
        return {"current_user": None, "pool": None, "is_commissioner": False}
    member = next(iter(user.memberships), None)
    pool = member.pool if member else None
    return {
        "current_user": user,
        "pool": pool,
        "is_commissioner": bool(pool and is_commissioner(db, user, pool)),
    }


@router.get("/terms")
def terms(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    return render(request, "legal/terms.html", {"settings": settings}, **_chrome(db, user))


@router.get("/privacy")
def privacy(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    return render(request, "legal/privacy.html", {"settings": settings}, **_chrome(db, user))


__all__ = ["router", "membership_for"]
