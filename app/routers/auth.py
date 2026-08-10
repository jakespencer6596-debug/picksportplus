"""Register, sign in, sign out, and joining a pool by code."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    flash,
    generate_join_code,
    get_current_user,
    hash_password,
    is_valid_email_format,
    login_user,
    logout_user,
    normalize_email,
    normalize_join_code,
    require_user,
    verify_password,
)
from app.config import settings
from app.db import get_db
from app.models import Pool, PoolMember, User
from app.templating import render

router = APIRouter(tags=["auth"])

MIN_PASSWORD_LEN = 8
# One character that is not a letter or digit. Permissive on purpose: any symbol counts,
# there is no narrow allow list to accidentally reject a reasonable choice.
_PASSWORD_SYMBOL_RE = re.compile(r"[^A-Za-z0-9]")


def _safe_next(raw: str | None) -> str:
    """Only allow same-site relative redirects, never an absolute URL."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/picks"
    return raw


def _find_pool_by_code(db: Session, code: str) -> Pool | None:
    code = normalize_join_code(code)
    if not code:
        return None
    # is_preview pools never gain a real member, by construction (Post-launch fixes, see
    # DECISIONS.md): excluded here so even a guessed or leaked preview join code can never
    # register or join against it.
    return db.scalar(
        select(Pool).where(func.upper(Pool.join_code) == code, Pool.is_preview.is_(False))
    )


def _find_pool_by_commissioner_code(db: Session, code: str) -> Pool | None:
    """Mirrors _find_pool_by_code exactly, but against Pool.commissioner_invite_code, a
    materially more powerful code that makes the registering user a commissioner rather than
    a member. Kept as a fully separate lookup, never folded into _find_pool_by_code with a
    "which column" flag, so there is never a code path where the two column names could be
    confused for one another. See DECISIONS.md, Post-launch fixes."""
    code = normalize_join_code(code)
    if not code:
        return None
    return db.scalar(select(Pool).where(func.upper(Pool.commissioner_invite_code) == code))


# Sign in --------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return RedirectResponse("/picks", status_code=303)
    return render(
        request,
        "auth/login.html",
        {"next": _safe_next(request.query_params.get("next")), "email": ""},
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/picks"),
    db: Session = Depends(get_db),
):
    address = normalize_email(email)
    user = db.scalar(select(User).where(User.email == address))
    # One message for both cases, so the form cannot be used to enumerate accounts.
    if user is None or not verify_password(password, user.password_hash):
        return render(
            request,
            "auth/login.html",
            {
                "error": "That email and password do not match an account.",
                "email": email,
                "next": _safe_next(next),
            },
            status_code=400,
        )
    if not user.is_active:
        return render(
            request,
            "auth/login.html",
            {
                "error": "That account is not active. Ask the commissioner to restore it.",
                "email": email,
                "next": _safe_next(next),
            },
            status_code=403,
        )

    login_user(request, user)
    flash(request, f"Welcome back, {user.display_name}.")
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


# Register -------------------------------------------------------------------


@router.get("/register")
def register_form(
    request: Request,
    user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user:
        return RedirectResponse("/picks", status_code=303)
    # A distinct query param from the player join code's ?code=, on purpose: a commissioner
    # invite link grants a materially more powerful role, and the two must never be
    # confusable with one another (Post-launch fixes, see DECISIONS.md). Only treated as a
    # commissioner link once it actually resolves to a real pool; an unknown or missing code
    # here falls straight back to today's plain registration form, no error shown, no
    # regression.
    commissioner_code = request.query_params.get("commissioner_code", "")
    commissioner_pool = (
        _find_pool_by_commissioner_code(db, commissioner_code) if commissioner_code else None
    )
    return render(
        request,
        "auth/register.html",
        {
            "open_registration": settings.open_registration,
            "join_code": request.query_params.get("code", ""),
            "commissioner_code": commissioner_code if commissioner_pool else "",
            "commissioner_pool": commissioner_pool,
            "form": {},
        },
    )


@router.post("/register")
def register_submit(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    join_code: str = Form(""),
    commissioner_code: str = Form(""),
    db: Session = Depends(get_db),
):
    form = {"display_name": display_name.strip(), "email": email, "join_code": join_code}
    address = normalize_email(email)
    errors: list[str] = []

    if not form["display_name"]:
        errors.append("Enter the name you want on the leaderboard.")
    elif len(form["display_name"]) > 80:
        errors.append("That display name is too long. Keep it under 80 characters.")
    if not is_valid_email_format(address):
        errors.append("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LEN or not _PASSWORD_SYMBOL_RE.search(password):
        errors.append(
            f"Use a password of at least {MIN_PASSWORD_LEN} characters, including one "
            "symbol (anything that is not a letter or a number)."
        )
    elif len(password.encode("utf-8")) > 72:
        errors.append("That password is too long. Keep it to 72 characters.")

    # A commissioner code and a plain join code are never both honored: exactly one of these
    # two ends up set. commissioner_code, once present at all, is authoritative and never
    # silently falls back to the join_code branch below, the same way an unknown join_code
    # is a hard error rather than a silent skip to open registration.
    pool: Pool | None = None
    commissioner_pool: Pool | None = None
    if commissioner_code.strip():
        commissioner_pool = _find_pool_by_commissioner_code(db, commissioner_code)
        if commissioner_pool is None:
            errors.append("That commissioner link is not valid. Check it with the site admin.")
    elif join_code.strip() or not settings.open_registration:
        pool = _find_pool_by_code(db, join_code)
        if pool is None:
            errors.append("That join code does not match a pool. Check it with the commissioner.")

    if not errors and db.scalar(select(User).where(User.email == address)) is not None:
        errors.append("An account already uses that email. Sign in instead.")

    if errors:
        return render(
            request,
            "auth/register.html",
            {
                "errors": errors,
                "form": form,
                "open_registration": settings.open_registration,
                "join_code": join_code,
                "commissioner_code": commissioner_code,
                "commissioner_pool": commissioner_pool,
            },
            status_code=400,
        )

    # role is always "player" here, never varied by this form, commissioner code or not: a
    # commissioner invite link only ever changes the PoolMember.role_in_pool row added below,
    # never User.role. See DECISIONS.md, Post-launch fixes.
    user = User(
        email=address,
        password_hash=hash_password(password),
        display_name=form["display_name"],
        role="player",
    )
    db.add(user)
    db.flush()

    if commissioner_pool is not None:
        db.add(
            PoolMember(pool_id=commissioner_pool.id, user_id=user.id, role_in_pool="commissioner")
        )
        db.commit()
        login_user(request, user)
        flash(
            request,
            f"You are the commissioner of {commissioner_pool.name}. Let's get your league set up.",
        )
        return RedirectResponse("/admin", status_code=303)

    if pool is not None:
        db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="member"))
    db.commit()

    login_user(request, user)
    if pool is not None:
        flash(request, f"You are in. Welcome to {pool.name}.")
        return RedirectResponse("/picks", status_code=303)
    flash(request, "Account created. Enter a join code to get into a pool.", "info")
    return RedirectResponse("/join", status_code=303)


# Joining an additional pool -------------------------------------------------


@router.get("/join")
def join_form(request: Request, user: User = Depends(require_user)):
    return render(request, "auth/join.html", {"form": {}}, current_user=user)


@router.post("/join")
def join_submit(
    request: Request,
    join_code: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    pool = _find_pool_by_code(db, join_code)
    if pool is None:
        return render(
            request,
            "auth/join.html",
            {
                "error": "That join code does not match a pool. Check it with the commissioner.",
                "form": {"join_code": join_code},
            },
            current_user=user,
            status_code=400,
        )
    existing = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == user.id)
    )
    if existing is None:
        db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="member"))
        db.commit()
        flash(request, f"You joined {pool.name}.")
    else:
        flash(request, f"You are already in {pool.name}.", "info")
    request.session["pid"] = pool.id
    return RedirectResponse("/picks", status_code=303)


__all__ = ["router", "generate_join_code"]
