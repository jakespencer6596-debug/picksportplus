"""Register, sign in, sign out, joining a pool by code, and password reset."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
from urllib.parse import urlencode

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
from app.models import PasswordResetToken, Pool, PoolMember, User, utcnow
from app.services import mail
from app.templating import render

router = APIRouter(tags=["auth"])

MIN_PASSWORD_LEN = 8
# One character that is not a letter or digit. Permissive on purpose: any symbol counts,
# there is no narrow allow list to accidentally reject a reasonable choice.
_PASSWORD_SYMBOL_RE = re.compile(r"[^A-Za-z0-9]")

# A password reset link is live for this long, from the moment it is emailed.
RESET_TOKEN_LIFETIME = dt.timedelta(hours=1)


def _password_errors(password: str) -> list[str]:
    """Shared with register_submit's own inline checks below, kept here as a small helper
    (Phase 7 remediation) so reset_password_submit enforces the exact same rule without
    duplicating the two conditions by hand."""
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LEN or not _PASSWORD_SYMBOL_RE.search(password):
        errors.append(
            f"Use a password of at least {MIN_PASSWORD_LEN} characters, including one "
            "symbol (anything that is not a letter or a number)."
        )
    elif len(password.encode("utf-8")) > 72:
        errors.append("That password is too long. Keep it to 72 characters.")
    return errors


def _hash_token(raw_token: str) -> str:
    """A fast, deterministic hash, deliberately not app.auth.hash_password's bcrypt: bcrypt's
    slow, salted hash exists to resist offline brute forcing of a low entropy human-chosen
    secret. A reset token is a 256 bit value from secrets.token_urlsafe(32), already far past
    brute forceable, so a fast hash is used instead, which is what lets _find_reset_token look
    a token up by an exact match rather than scanning and verifying every pending row by hand.
    See PasswordResetToken's own docstring in app/models.py."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _find_reset_token(db: Session, raw_token: str) -> PasswordResetToken | None:
    raw_token = (raw_token or "").strip()
    if not raw_token:
        return None
    return db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash_token(raw_token))
    )


def _reset_token_is_live(token: PasswordResetToken) -> bool:
    if token.used_at is not None:
        return False
    expires_at = token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    return expires_at >= utcnow()


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
    errors.extend(_password_errors(password))

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
    elif join_code.strip():
        pool = _find_pool_by_code(db, join_code)
        if pool is None:
            errors.append("That join code does not match a pool. Check it with the commissioner.")
    # A blank join_code (and no commissioner_code) is always allowed, regardless of
    # open_registration: the account is created with no pool, and lands on the read only
    # preview slate until a real join code is entered later (Post-launch fixes, see
    # DECISIONS.md). open_registration historically gated whether an account could be
    # created at all without a code; now that a codeless account is always a safe, poolless
    # preview rather than membership in anything, there is nothing left for it to block here.

    if not errors:
        existing = db.scalar(select(User).where(User.email == address))
        if existing is not None:
            if commissioner_pool is not None:
                # A real account already exists for this address: the plain "sign in
                # instead" error would otherwise be a dead end, since register_submit
                # never attaches a commissioner PoolMember to an account it does not
                # itself create. Send them to sign in with the commissioner code carried
                # through, so /accept-commissioner can finish the job once they are
                # authenticated. See DECISIONS.md.
                flash(
                    request,
                    f"An account already uses {address}. Sign in to accept the "
                    f"commissioner invite for {commissioner_pool.name}.",
                    "info",
                )
                next_path = (
                    f"/accept-commissioner?code={commissioner_pool.commissioner_invite_code}"
                )
                return RedirectResponse(f"/login?{urlencode({'next': next_path})}", status_code=303)
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
        return RedirectResponse("/league", status_code=303)

    if pool is not None:
        db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="member"))
    db.commit()

    login_user(request, user)
    if pool is not None:
        flash(request, f"You are in. Welcome to {pool.name}.")
        return RedirectResponse("/picks", status_code=303)
    flash(request, "Account created. Look around, then join a league whenever you're ready.")
    return RedirectResponse("/picks", status_code=303)


# Accepting a commissioner invite as an existing account ---------------------


@router.get("/accept-commissioner")
def accept_commissioner_form(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
):
    """The other half of the commissioner invite link (register_form/register_submit above
    handle the brand new account case). Reached either directly from the invite email, or
    via /login?next=... once register_submit redirects an already-registered address here.
    Public (no require_user): a signed out visitor sees a sign-in-or-register choice rather
    than being forced through one path. See DECISIONS.md."""
    code = request.query_params.get("code", "")
    pool = _find_pool_by_commissioner_code(db, code)
    if pool is None:
        flash(
            request, "That commissioner link is not valid. Check it with the site admin.", "error"
        )
        return RedirectResponse("/login", status_code=303)
    if user is not None and user.is_admin:
        # Structurally can never hold a PoolMember row (Post-launch fixes, see
        # DECISIONS.md): a site admin already reaches every league as commissioner from
        # /site/leagues, so this link has nothing left to do for them.
        flash(
            request,
            "Site admins already act as commissioner for every league from /site/leagues; "
            "this invite link is for a regular player account.",
            "info",
        )
        return RedirectResponse("/site/leagues", status_code=303)
    return render(
        request,
        "auth/accept_commissioner.html",
        {"pool": pool, "code": code},
        current_user=user,
    )


@router.post("/accept-commissioner")
def accept_commissioner_submit(
    request: Request,
    code: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    pool = _find_pool_by_commissioner_code(db, code)
    if pool is None:
        flash(
            request, "That commissioner link is not valid. Check it with the site admin.", "error"
        )
        return RedirectResponse("/login", status_code=303)
    if user.is_admin:
        flash(
            request,
            "Site admins already act as commissioner for every league from /site/leagues.",
            "info",
        )
        return RedirectResponse("/site/leagues", status_code=303)

    member = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == user.id)
    )
    if member is None:
        db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool="commissioner"))
        db.commit()
        flash(request, f"You are the commissioner of {pool.name}. Let's get your league set up.")
    elif member.role_in_pool == "commissioner":
        flash(request, f"You are already commissioner of {pool.name}.")
    else:
        # A pre-existing membership (player or co-commissioner) is promoted in place rather
        # than rejected: the invite is unambiguous about the intended role, and there is
        # nothing to lose by honoring it.
        member.role_in_pool = "commissioner"
        db.commit()
        flash(request, f"You are now commissioner of {pool.name}.")
    return RedirectResponse("/league", status_code=303)


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


# Password reset -------------------------------------------------------------


@router.get("/forgot-password")
def forgot_password_form(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return RedirectResponse("/picks", status_code=303)
    return render(request, "auth/forgot_password.html", {"email": ""})


@router.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    """Always the same message, whether or not the address matches an account, and whether or
    not the email actually sent (mail turned off, misconfigured, or the provider call itself
    failing all take the same silent-to-the-visitor path here), matching login_submit's own
    "one message for both cases" convention so this form can never be used to enumerate
    accounts. A real send failure is not lost, it is still written to MailLog by
    app.services.mail.send and visible to the site admin at /site/mail; see DECISIONS.md,
    Phase 7, for why this one call site deliberately does not surface the failure to the
    caller the way every other mail call site in this app does."""
    address = normalize_email(email)
    generic_message = "If an account exists for that email, a reset link was sent."

    target = None
    if is_valid_email_format(address):
        target = db.scalar(select(User).where(User.email == address, User.is_active.is_(True)))

    if target is not None:
        raw_token = secrets.token_urlsafe(32)
        db.add(
            PasswordResetToken(
                user_id=target.id,
                token_hash=_hash_token(raw_token),
                expires_at=utcnow() + RESET_TOKEN_LIFETIME,
            )
        )
        db.flush()
        link = f"{settings.base_url}/reset-password?token={raw_token}"
        subject = "Reset your PickSportPlus password"
        body = (
            "Hey,\n\n"
            f"Someone (hopefully you) asked to reset the password for {target.email} on "
            "PickSportPlus. This link works once, and only for the next hour:\n\n"
            f"{link}\n\n"
            "If this was not you, ignore this message. Your password stays the same."
        )
        try:
            mail.send(
                db,
                to=target.email,
                subject=subject,
                html=mail.text_to_html(body),
                text=body,
                kind="password_reset",
                actor_key=f"email:{target.email}",
            )
        except mail.MailError:
            pass

    db.commit()
    flash(request, generic_message)
    return RedirectResponse("/login", status_code=303)


@router.get("/reset-password")
def reset_password_form(
    request: Request, token: str = "", user: User | None = Depends(get_current_user)
):
    if user:
        return RedirectResponse("/picks", status_code=303)
    return render(request, "auth/reset_password.html", {"token": token})


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    row = _find_reset_token(db, token)
    if row is None:
        return render(
            request,
            "auth/reset_password.html",
            {"token": token, "error": "That reset link is not valid. Request a new one."},
            status_code=400,
        )
    if row.used_at is not None:
        return render(
            request,
            "auth/reset_password.html",
            {"token": token, "error": "That reset link has already been used. Request a new one."},
            status_code=400,
        )
    if not _reset_token_is_live(row):
        return render(
            request,
            "auth/reset_password.html",
            {"token": token, "error": "That reset link has expired. Request a new one."},
            status_code=400,
        )

    errors = _password_errors(password)
    if errors:
        return render(
            request,
            "auth/reset_password.html",
            {"token": token, "error": errors[0]},
            status_code=400,
        )

    target = db.get(User, row.user_id)
    if target is None:
        return render(
            request,
            "auth/reset_password.html",
            {"token": token, "error": "That reset link is not valid. Request a new one."},
            status_code=400,
        )

    target.password_hash = hash_password(password)
    row.used_at = utcnow()
    db.commit()
    flash(request, "Password updated. Sign in with your new password.")
    return RedirectResponse("/login", status_code=303)


__all__ = ["router", "generate_join_code"]
