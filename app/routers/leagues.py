"""Site admin league management: list every pool in the system, create new ones, and
"view as commissioner" to jump into one.

Everything here sits behind require_admin, not require_commissioner. A pool's own
commissioner, even one who runs several pools, never reaches this file: being the
commissioner of your own league does not make you a site admin. Kept out of
app/routers/admin.py on purpose, whose own module docstring says "Everything here sits
behind require_commissioner" -- true today, and a second permission boundary living in the
same file would either break that statement or bury a require_admin route in a file a reader
scans expecting only one gate. Mounted at /site/leagues, its own top-level namespace
(Phase 4 remediation, see DECISIONS.md), separate from the commissioner-scoped /league
section: a commissioner's own tools and a site admin's platform-wide tools no longer share
a URL prefix or a nav label.

Once "view as commissioner" (POST /site/leagues/{pool_id}/view-as) sets the session's active
pool, every existing require_commissioner route in app/routers/admin.py already treats the
admin as that pool's commissioner (app.auth.is_commissioner already returns True for any
user.is_admin, unconditionally). Nothing here duplicates the settings, slate, members or
payouts screens: this file only ever adds the league list, league creation, and the view-as
switch.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_POOL_KEY,
    flash,
    generate_join_code,
    is_valid_email_format,
    normalize_email,
    normalize_join_code,
    require_admin,
)
from app.config import settings
from app.db import get_db
from app.models import Pool, PoolMember, User
from app.services import mail
from app.services.calendar import default_week1_anchor_date
from app.templating import get_zone, render

router = APIRouter(prefix="/site/leagues", tags=["admin-leagues"])

# Same defaults seed_admin (app/cli.py) writes for the one pool it creates. A league made
# here starts identical, and the commissioner tunes games-per-week/targets later from that
# pool's own existing /league/settings, never re-entered on this creation form.
DEFAULT_NUM_GAMES_PER_WEEK = 20
DEFAULT_TARGET_NFL = 8
DEFAULT_TARGET_NCAAF = 12

TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "UTC",
]


def _redirect(target: str = "/site/leagues") -> RedirectResponse:
    return RedirectResponse(target, status_code=303)


def _base(user: User) -> dict:
    # No single pool is "active" on these pages (the leagues list spans every pool), so
    # pool is None here. is_commissioner is hard set True rather than run through
    # app.auth.is_commissioner (which needs a concrete pool to check membership against):
    # a global admin is a commissioner everywhere, pool or no pool, which is exactly why the
    # nav's existing "League" link condition (is_commissioner) should still light up here.
    # active_nav is deliberately its own value, distinct from "league", so the
    # viewing-as-commissioner banner (wired in base.html off the /league section) never
    # renders on this section: this portal isn't a view of any single pool.
    return {
        "current_user": user,
        "pool": None,
        "is_commissioner": True,
        "active_nav": "site_leagues",
        # Absolute origin for the commissioner invite link, built the same way
        # app/routers/public.py already builds base_url for pricing.html's mailto link.
        "base_url": settings.base_url,
    }


def _fresh_commissioner_invite_code(db: Session, exclude_pool_id: int | None = None) -> str:
    """A commissioner_invite_code that collides with nobody else's, same collision-avoidance
    shape as app/routers/admin.py's rotate_join_code (10 tries against the real column,
    reusing generate_join_code, never a second generator)."""
    for _ in range(10):
        code = generate_join_code()
        query = select(Pool).where(func.upper(Pool.commissioner_invite_code) == code)
        if exclude_pool_id is not None:
            query = query.where(Pool.id != exclude_pool_id)
        if db.scalar(query) is None:
            return code
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not generate a commissioner invite code."
    )


def _parse_commissioner_emails(text: str) -> list[str]:
    """One email per line (blank lines skipped), normalized. Order preserved, de-duplicated."""
    seen: list[str] = []
    for raw_line in text.splitlines():
        email = normalize_email(raw_line)
        if email and email not in seen:
            seen.append(email)
    return seen


@router.get("")
def leagues_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    # is_preview pools are never a customer league (Post-launch fixes, see DECISIONS.md): the
    # one hidden preview pool never appears here, never gets a commissioner from this page,
    # and is never counted alongside the real leagues a site admin manages.
    pools = list(db.scalars(select(Pool).where(Pool.is_preview.is_(False)).order_by(Pool.name)))
    member_counts = dict(
        db.execute(
            select(PoolMember.pool_id, func.count(PoolMember.id)).group_by(PoolMember.pool_id)
        ).all()
    )
    commissioners_by_pool: dict[int, list[User]] = {}
    rows = db.execute(
        select(PoolMember, User)
        .join(User, User.id == PoolMember.user_id)
        .where(PoolMember.role_in_pool == "commissioner")
        .order_by(User.display_name)
    )
    for member, player in rows:
        commissioners_by_pool.setdefault(member.pool_id, []).append(player)

    return render(
        request,
        "admin/leagues.html",
        {
            "pools": pools,
            "member_counts": member_counts,
            "commissioners_by_pool": commissioners_by_pool,
        },
        **_base(user),
    )


@router.get("/new")
def league_new_page(
    request: Request,
    user: User = Depends(require_admin),
):
    default_season_year = dt.date.today().year
    return render(
        request,
        "admin/league_new.html",
        {
            "join_code_prefill": generate_join_code(),
            "default_season_year": default_season_year,
            "default_anchor_date": default_week1_anchor_date(default_season_year).isoformat(),
            "timezones": TIMEZONES,
            "num_games_per_week": DEFAULT_NUM_GAMES_PER_WEEK,
            "target_nfl": DEFAULT_TARGET_NFL,
            "target_ncaaf": DEFAULT_TARGET_NCAAF,
        },
        **_base(user),
    )


@router.post("/new")
def league_new_save(
    request: Request,
    name: str = Form(...),
    join_code: str = Form(...),
    season_year: int = Form(...),
    timezone: str = Form(...),
    week1_anchor_date: str = Form(""),
    commissioner_emails: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    errors: list[str] = []
    name = name.strip()
    if not name:
        errors.append("The league needs a name.")

    code = normalize_join_code(join_code)
    if len(code) < 4:
        errors.append("A join code needs at least 4 characters.")
    elif db.scalar(select(Pool).where(func.upper(Pool.join_code) == code)) is not None:
        errors.append("Another league already uses that join code.")

    try:
        get_zone(timezone)
    except Exception:
        errors.append("That timezone is not recognised.")

    # Required (Phase 2 remediation, see DECISIONS.md): a blank anchor date is what let a
    # brand new league fall back to sending its own week number straight to ESPN for both
    # leagues, which produced a slate spanning two calendar weeks with the same team twice.
    anchor_date: dt.date | None = None
    week1_anchor_date = week1_anchor_date.strip()
    if not week1_anchor_date:
        errors.append(
            "Set the week 1 anchor date. Without it the tool cannot tell which NFL and "
            "college weeks belong together."
        )
    else:
        try:
            anchor_date = dt.date.fromisoformat(week1_anchor_date)
        except ValueError:
            errors.append("Week 1 anchor date is not a valid date.")
        else:
            if anchor_date.weekday() != 5:
                errors.append("Week 1 anchor date must be a Saturday.")

    if errors:
        for message in errors:
            flash(request, message, "error")
        return _redirect("/site/leagues/new")

    pool = Pool(
        name=name,
        join_code=code,
        commissioner_invite_code=_fresh_commissioner_invite_code(db),
        season_year=season_year,
        num_games_per_week=DEFAULT_NUM_GAMES_PER_WEEK,
        target_nfl=DEFAULT_TARGET_NFL,
        target_ncaaf=DEFAULT_TARGET_NCAAF,
        sports=["nfl", "ncaaf"],
        timezone=timezone,
        current_week=1,
        week1_anchor_date=anchor_date,
    )
    db.add(pool)
    db.flush()

    emails = _parse_commissioner_emails(commissioner_emails)
    attached: list[str] = []
    missing: list[str] = []
    is_admin_email: list[str] = []
    for email in emails:
        found = db.scalar(select(User).where(User.email == email))
        if found is None:
            missing.append(email)
            continue
        if found.is_admin:
            # The site admin can never hold a PoolMember row, structurally, not just by
            # convention (Post-launch fixes, see DECISIONS.md). Skip this one address and
            # keep processing the rest of the submission rather than failing the whole form.
            is_admin_email.append(email)
            continue
        db.add(PoolMember(pool_id=pool.id, user_id=found.id, role_in_pool="commissioner"))
        attached.append(found.display_name)

    db.commit()

    flash(request, f"Created {pool.name} with join code {pool.join_code}.")
    if attached:
        flash(request, f"Added as commissioner: {', '.join(attached)}.")
    if missing:
        flash(
            request,
            "No account found for: " + ", ".join(missing) + ". "
            "Only existing users can be attached here; promote someone from the pool's "
            "Members page once they have registered.",
            "info",
        )
    for email in is_admin_email:
        flash(
            request,
            f"{email} is the site admin and can't be added as a commissioner.",
            "error",
        )
    return _redirect("/site/leagues")


@router.post("/{pool_id}/commissioner-code")
def rotate_commissioner_invite_code(
    request: Request,
    pool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Site-admin only, on purpose: creating a commissioner is the more powerful of the two
    invite links (Post-launch fixes, see DECISIONS.md), so unlike the player join code, which
    a pool's own commissioner can rotate from /league/members, only the site admin can rotate
    this one. Never touches Pool.join_code, the same way join code rotation never touches
    this column."""
    pool = db.get(Pool, pool_id)
    if pool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such league.")
    pool.commissioner_invite_code = _fresh_commissioner_invite_code(db, exclude_pool_id=pool.id)
    db.commit()
    flash(request, f"New commissioner invite link generated for {pool.name}.")
    return _redirect("/site/leagues")


@router.post("/{pool_id}/commissioner-code/set")
def set_commissioner_invite_code(
    request: Request,
    pool_id: int,
    commissioner_invite_code: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    pool = db.get(Pool, pool_id)
    if pool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such league.")
    code = normalize_join_code(commissioner_invite_code)
    if len(code) < 4:
        flash(request, "A commissioner invite code needs at least 4 characters.", "error")
        return _redirect("/site/leagues")
    clash = db.scalar(
        select(Pool).where(func.upper(Pool.commissioner_invite_code) == code, Pool.id != pool.id)
    )
    if clash is not None:
        flash(request, "Another league already uses that commissioner invite code.", "error")
        return _redirect("/site/leagues")
    pool.commissioner_invite_code = code
    db.commit()
    flash(request, f"Commissioner invite code for {pool.name} set to {code}.")
    return _redirect("/site/leagues")


@router.post("/{pool_id}/commissioner-code/email")
def email_commissioner_invite(
    request: Request,
    pool_id: int,
    email: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Site admin only (Phase 7 remediation, see DECISIONS.md), sends this one league's
    commissioner invite link to one address. Sits next to the existing copy-link panel on
    /site/leagues, never replaces it: on any failure (mail off, not configured, rate limited,
    or the provider call itself failing) the flash below says so and the copy-link panel is
    still right there on the same page, unchanged."""
    pool = db.get(Pool, pool_id)
    if pool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such league.")
    address = normalize_email(email)
    if not is_valid_email_format(address):
        flash(request, "Enter a valid email address.", "error")
        return _redirect("/site/leagues")

    if not pool.commissioner_invite_code:
        pool.commissioner_invite_code = _fresh_commissioner_invite_code(db, exclude_pool_id=pool.id)
        db.flush()

    # /accept-commissioner, not straight to /register: that route works for a brand new
    # account and an already-registered address alike (sign in, then get attached), where
    # /register?commissioner_code=... alone only ever handled the brand new case. See
    # DECISIONS.md.
    link = f"{settings.base_url}/accept-commissioner?code={pool.commissioner_invite_code}"
    subject = f"Thanks for joining PickSportPlus, {pool.name} is ready for you"
    body = (
        "Hey,\n\n"
        f"Thanks for joining PickSportPlus and registering to create a league. {pool.name} is "
        "ready for you to set up and run as commissioner.\n\n"
        f"Sign in if you already have an account, or create one here:\n\n{link}\n\n"
        "See you on the leaderboard."
    )
    try:
        mail.send(
            db,
            to=address,
            subject=subject,
            html=mail.text_to_html(body),
            text=body,
            kind="commissioner_invite",
            actor_key=f"user:{user.id}",
        )
    except mail.MailError as exc:
        db.commit()
        flash(request, str(exc), "error")
        flash(request, "The invite link below still works. Copy it and send it yourself.", "info")
    else:
        db.commit()
        flash(request, f"Commissioner invite emailed to {address}.")
    return _redirect("/site/leagues")


@router.post("/{pool_id}/view-as")
def view_as(
    request: Request,
    pool_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    pool = db.get(Pool, pool_id)
    if pool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such league.")
    request.session[SESSION_POOL_KEY] = pool.id
    flash(request, f"Viewing {pool.name} as commissioner.")
    return RedirectResponse("/league", status_code=303)


__all__ = ["router"]
