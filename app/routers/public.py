"""Public marketing pages: pricing, how to use, and contact.

Like app/routers/legal.py, these are deliberately public. A visitor deciding whether to
sign up has to be able to read them before they have an account, so there is no
require_user here, only the same signed-in-or-not chrome switch legal.py already uses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    flash,
    get_current_user,
    is_commissioner,
    is_valid_email_format,
    normalize_email,
)
from app.config import settings
from app.db import get_db
from app.models import ContactSubmission, User
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
    return render(request, "public/contact.html", {"form": {}}, **_chrome(db, user))


@router.post("/contact")
def contact_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    message: str = Form(""),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """Store a lead. Nothing here sends an email or a text message to anyone: the 24 hour
    reply promise is fulfilled by a human reading GET /admin/contacts and writing back over
    their own email client, not by this route."""
    form = {"name": name.strip(), "email": email, "message": message.strip()}
    address = normalize_email(email)
    errors: list[str] = []
    if not form["name"]:
        errors.append("Enter your name.")
    if not is_valid_email_format(address):
        errors.append("Enter a valid email address.")
    if not form["message"]:
        errors.append("Enter a message so we know how to help.")

    if errors:
        return render(
            request,
            "public/contact.html",
            {"errors": errors, "form": form},
            status_code=400,
            **_chrome(db, user),
        )

    db.add(ContactSubmission(name=form["name"], email=address, message=form["message"]))
    db.commit()
    flash(request, "Thanks, that's in. We will get back to you within 24 hours.")
    return RedirectResponse("/contact", status_code=303)
