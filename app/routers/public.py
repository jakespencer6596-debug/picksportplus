"""Public marketing pages: pricing, how to use, and contact.

Like app/routers/legal.py, these are deliberately public. A visitor deciding whether to
sign up has to be able to read them before they have an account, so there is no
require_user here, only the same signed-in-or-not chrome switch legal.py already uses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import get_current_user, is_commissioner
from app.config import settings
from app.db import get_db
from app.models import User
from app.templating import render

router = APIRouter(tags=["public"])


def _chrome(db: Session, user: User | None) -> dict:
    """Show the signed-in navigation when there is a session, and the public bar otherwise."""
    if user is None:
        return {"current_user": None, "pool": None, "is_commissioner": False}
    member = next(iter(user.memberships), None)
    pool = member.pool if member else None
    return {
        "current_user": user,
        "pool": pool,
        "is_commissioner": bool(pool and is_commissioner(db, user, pool)),
    }


@router.get("/pricing")
def pricing(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    return render(
        request,
        "public/pricing.html",
        {"base_url": settings.base_url},
        **_chrome(db, user),
    )


@router.get("/how-it-works")
def how_it_works(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    return render(request, "public/how_it_works.html", {}, **_chrome(db, user))


@router.get("/contact")
def contact(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    return render(request, "public/contact.html", {}, **_chrome(db, user))
