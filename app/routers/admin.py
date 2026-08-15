"""Commissioner tools: pool settings, the slate editor, members, and manual job triggers.

Everything here sits behind require_commissioner, with three exceptions. switch_league (POST
/league/switch-league) deliberately is not: require_commissioner resolves the pool from
whatever is already active in session, and the whole point of that route is picking a
different one. It is gated by require_user plus its own direct check, against the requested
pool_id, for a real PoolMember row with role_in_pool in ("commissioner", "co_commissioner"). A
regular player still cannot reach it for any pool they do not commission.

co_commissioner_accept and co_commissioner_decline (POST /league/co-commissioner/accept and
/decline, Post-launch fixes: co-commissioner self-service invites with confirmation) are the
other two: the whole point is that the invited person is still a plain "member" at the moment
they act, not yet any kind of commissioner, so require_commissioner would refuse them. Both are
gated by require_user plus get_active_pool and act only on the caller's own PoolMember row,
never anyone else's.

A handful of routes (member_role, the co-commissioner invite/cancel routes, and the
commissioner invite link) sit behind the narrower require_full_commissioner instead of
require_commissioner: a co-commissioner can operate this pool like a commissioner everywhere
else, but never manage anyone's commissioner status. See app.auth.require_full_commissioner.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_POOL_KEY,
    Pool,
    flash,
    generate_join_code,
    get_active_pool,
    is_full_commissioner,
    membership_for,
    normalize_join_code,
    require_commissioner,
    require_full_commissioner,
    require_user,
)
from app.config import Settings, settings
from app.db import get_db
from app.models import (
    SCORING_MODES,
    Game,
    Pick,
    PoolMember,
    User,
    Week,
    utcnow,
)
from app.providers.http import get_platform_settings, provider_warnings
from app.providers.teams import canonical_key, display_name
from app.routers.leagues import _fresh_commissioner_invite_code, _parse_commissioner_emails
from app.services import ingest, mail
from app.templating import get_zone, render

router = APIRouter(prefix="/league", tags=["admin"])

LEAGUE_LABELS = {"nfl": "NFL", "ncaaf": "College"}


def _redirect(target: str = "/league") -> RedirectResponse:
    return RedirectResponse(target, status_code=303)


def _redirect_or_hx(request: Request, target: str) -> RedirectResponse | Response:
    """A plain 303 for a normal form post; a real, full page navigation for an htmx one.

    htmx follows a plain 303 transparently at the XHR level (per its own docs), which means
    the *redirected-to* page's full HTML, not just its status, comes back as this request's
    response, and htmx would then swap that whole page into whatever small hx-target issued
    the request, exactly the trap app/routers/payouts.py's own module docstring documents. The
    HX-Redirect response header sidesteps it entirely: htmx sees the header and does a real
    `window.location` navigation instead of a swap, the same pattern
    app/routers/picks.py's picks_lock/picks_unlock already use. Used by slate_build (Phase 6
    remediation, see DECISIONS.md) so the build form's loading state, wired up as a normal
    htmx request, still lands on a full, freshly rendered /league/slate page with the flash
    message on it, exactly like the non-JS form post always has."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": target})
    return _redirect(target)


def _week_or_none(db: Session, pool: Pool, week_number: int | None) -> Week | None:
    query = select(Week).where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
    if week_number is not None:
        return db.scalar(query.where(Week.week_number == week_number))
    return db.scalar(
        query.where(Week.status.in_(("draft", "open", "locked"))).order_by(Week.week_number.desc())
    ) or db.scalar(query.order_by(Week.week_number.desc()))


def _commissioner_pools(db: Session, user: User) -> list[Pool]:
    """Every pool this user really commissions (a PoolMember row, role_in_pool ==
    "commissioner" or "co_commissioner"), not the global admin's "view as commissioner" case,
    which never leaves a row behind. Used only to decide whether the league switcher has
    anything to switch between; a site admin always gets an empty list here, since
    /site/leagues, not this switcher, is how an admin moves between pools. A co-commissioner
    running more than one pool sees the same switcher a full commissioner would (Post-launch
    fixes), since is_commissioner already treats both roles as equally able to operate a
    pool."""
    if user.is_admin:
        return []
    return list(
        db.scalars(
            select(Pool)
            .join(PoolMember, PoolMember.pool_id == Pool.id)
            .where(
                PoolMember.user_id == user.id,
                PoolMember.role_in_pool.in_(("commissioner", "co_commissioner")),
            )
            .order_by(Pool.name)
        )
    )


def _base(db: Session, user: User, pool: Pool) -> dict:
    return {
        "current_user": user,
        "pool": pool,
        "is_commissioner": True,
        "active_nav": "league",
        # Absolute origin for the player invite link and its mailto template, built the same
        # way app/routers/public.py already builds base_url for pricing.html's mailto link.
        "base_url": settings.base_url,
        # Only populated for a real, non-admin commissioner (Post-launch fixes, see
        # DECISIONS.md); admin/index.html only renders the league switcher when this has more
        # than one pool in it, so the common single-league case shows nothing extra.
        "commissioner_pools": _commissioner_pools(db, user),
        # True for the site admin or a real full commissioner, never a co-commissioner
        # (Post-launch fixes). Gates the commissioner invite link panel and the role-management
        # actions on admin/members.html: a co-commissioner reaches every admin page but sees
        # none of that.
        "can_manage_commissioners": is_full_commissioner(db, user, pool),
    }


# Rivalries --------------------------------------------------------------------

# Splits "Ohio State vs Michigan" (case insensitive, "vs" or "vs.") into its two names.
_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def _parse_rivalries(text: str) -> list[list[str]]:
    """One "Team A vs Team B" matchup per line into canonical key pairs.

    Blank lines and lines with no recognisable "vs" separator are skipped rather than
    rejected, since a rivalry list is a low stakes convenience setting, not a place the
    commissioner needs a form error blocking the rest of their save over one typo. Every
    name is resolved via canonical_key(name, "ncaaf"): every rivalry named in the brief, and
    every one in the seeded default list, is a college matchup, so this form does not (yet)
    support NFL rivalries. canonical_key never raises and falls back to a slug of whatever
    was typed, so an unrecognised team name is stored rather than dropped; it simply will
    not match any real game until the spelling is fixed.
    """
    pairs: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = _VS_RE.split(line, maxsplit=1)
        if len(parts) != 2:
            continue
        left, right = (part.strip() for part in parts)
        if not left or not right:
            continue
        pairs.append([canonical_key(left, "ncaaf"), canonical_key(right, "ncaaf")])
    return pairs


def _rivalries_text(rivalries: list[list[str]] | None) -> str:
    """The reverse of _parse_rivalries, for the settings form's textarea."""
    lines: list[str] = []
    for pair in rivalries or []:
        if len(pair) != 2:
            continue
        a, b = pair
        lines.append(f"{display_name(a) or a} vs {display_name(b) or b}")
    return "\n".join(lines)


# Dashboard ------------------------------------------------------------------


@router.get("")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    weeks = list(
        db.scalars(
            select(Week)
            .where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
            .order_by(Week.week_number.desc())
        )
    )
    counts = {
        w.id: db.scalar(
            select(func.count(Game.id)).where(Game.week_id == w.id, Game.in_slate.is_(True))
        )
        or 0
        for w in weeks
    }
    pick_counts = {
        w.id: db.scalar(select(func.count(func.distinct(Pick.user_id))).where(Pick.week_id == w.id))
        or 0
        for w in weeks
    }
    member_count = (
        db.scalar(select(func.count(PoolMember.id)).where(PoolMember.pool_id == pool.id)) or 0
    )

    return render(
        request,
        "admin/index.html",
        {
            "weeks": weeks,
            "slate_counts": counts,
            "pick_counts": pick_counts,
            "member_count": member_count,
        },
        **_base(db, user, pool),
    )


@router.post("/switch-league")
def switch_league(
    request: Request,
    pool_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """A real commissioner of more than one league picking which one is active. Deliberately
    not gated by require_commissioner, which resolves the pool from the session's current
    active pool: the whole point here is choosing a different one. Instead this checks the
    requested pool_id directly against a real PoolMember row, so a commissioner of pool A
    cannot switch into pool B just by knowing its id. Separate concept from the site admin's
    "view as commissioner" (POST /site/leagues/{pool_id}/view-as, app/routers/leagues.py):
    that lets an admin borrow commissioner powers for a league they don't run; this lets an
    actual commissioner move between leagues they do. A co-commissioner counts here too
    (Post-launch fixes), same as a full commissioner, since operating a pool day to day is the
    bar for this switch, not managing its commissioner roster."""
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == pool_id,
            PoolMember.user_id == user.id,
            PoolMember.role_in_pool.in_(("commissioner", "co_commissioner")),
        )
    )
    if member is None:
        flash(request, "You don't commission that league.", "error")
        return _redirect("/league")
    request.session[SESSION_POOL_KEY] = pool_id
    return _redirect("/league")


# Pool settings --------------------------------------------------------------


@router.get("/settings")
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    return render(
        request,
        "admin/settings.html",
        {
            "min_slate": Settings.MIN_SLATE,
            "max_slate": Settings.MAX_SLATE,
            "env_open_registration": settings.open_registration,
            "scoring_modes": SCORING_MODES,
            "rivalries_text": _rivalries_text(pool.rivalries),
            "timezones": [
                "America/New_York",
                "America/Chicago",
                "America/Denver",
                "America/Los_Angeles",
                "America/Phoenix",
                "UTC",
            ],
        },
        **_base(db, user, pool),
    )


@router.post("/settings")
def settings_save(
    request: Request,
    name: str = Form(...),
    season_year: int = Form(...),
    timezone: str = Form(...),
    num_games_per_week: int = Form(...),
    target_nfl: int = Form(...),
    target_ncaaf: int = Form(...),
    picks_required: int = Form(...),
    scoring_mode: str = Form(...),
    scenarios_min_final_games: int = Form(...),
    scenarios_min_remaining_games: int = Form(...),
    auto_publish: str = Form(""),
    open_registration: str = Form(""),
    notify_week_published: str = Form(""),
    sports_nfl: str = Form(""),
    sports_ncaaf: str = Form(""),
    week1_anchor_date: str = Form(""),
    rivalries: str = Form(""),
    entry_fee: str = Form(""),
    venmo_handle: str = Form(""),
    payment_required_to_pick: str = Form(""),
    payment_note: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    errors: list[str] = []
    name = name.strip()
    if not name:
        errors.append("The league needs a name.")
    if not Settings.MIN_SLATE <= num_games_per_week <= Settings.MAX_SLATE:
        errors.append(
            f"Games per week must be between {Settings.MIN_SLATE} and {Settings.MAX_SLATE}."
        )
    if target_nfl < 0 or target_ncaaf < 0:
        errors.append("League counts cannot be negative.")
    if not 1 <= picks_required <= num_games_per_week:
        errors.append(
            f"Picks required must be between 1 and games per week ({num_games_per_week})."
        )
    if scoring_mode not in SCORING_MODES:
        errors.append("Scoring mode must be either inverse or standard.")
    if scenarios_min_final_games < 0:
        errors.append("Scenarios minimum final games cannot be negative.")
    if scenarios_min_remaining_games < 1:
        errors.append("Scenarios minimum remaining games must be at least 1.")
    sports = [s for s, on in (("nfl", sports_nfl), ("ncaaf", sports_ncaaf)) if on]
    if not sports:
        errors.append("Pick at least one league.")
    try:
        get_zone(timezone)
    except Exception:
        errors.append("That timezone is not recognised.")

    anchor_date: dt.date | None = pool.week1_anchor_date
    week1_anchor_date = week1_anchor_date.strip()
    if week1_anchor_date:
        try:
            anchor_date = dt.date.fromisoformat(week1_anchor_date)
        except ValueError:
            errors.append("Week 1 anchor date is not a valid date.")
        else:
            if anchor_date.weekday() != 5:
                errors.append("Week 1 anchor date must be a Saturday.")
    else:
        anchor_date = None

    fee_value: Decimal | None
    entry_fee = entry_fee.strip()
    if entry_fee:
        try:
            fee_value = Decimal(entry_fee)
        except InvalidOperation:
            errors.append("Entry fee must be a number.")
            fee_value = None
        else:
            if fee_value < 0:
                errors.append("Entry fee cannot be negative.")
    else:
        # Blank clears it back to unset, never to a hard coded 0: a pool with no real fee
        # entered yet has no fee, not a free fee.
        fee_value = None

    if errors:
        for message in errors:
            flash(request, message, "error")
        return _redirect("/league/settings")

    pool.name = name
    pool.season_year = season_year
    pool.timezone = timezone
    pool.num_games_per_week = num_games_per_week
    pool.target_nfl = target_nfl
    pool.target_ncaaf = target_ncaaf
    pool.picks_required = picks_required
    pool.scoring_mode = scoring_mode
    pool.scenarios_min_final_games = scenarios_min_final_games
    pool.scenarios_min_remaining_games = scenarios_min_remaining_games
    pool.sports = sports
    pool.auto_publish = bool(auto_publish)
    pool.open_registration = bool(open_registration)
    pool.notify_week_published = bool(notify_week_published)
    pool.week1_anchor_date = anchor_date
    pool.rivalries = _parse_rivalries(rivalries)
    pool.entry_fee = fee_value
    pool.venmo_handle = venmo_handle.strip() or None
    pool.payment_required_to_pick = bool(payment_required_to_pick)
    pool.payment_note = payment_note.strip() or None
    db.commit()

    total = sum(pool.league_targets.values())
    if total != pool.num_games_per_week:
        flash(
            request,
            f"Saved. Your league counts add up to {total} but the total is "
            f"{pool.num_games_per_week}, so the slate is topped up or trimmed by closeness.",
            "info",
        )
    else:
        flash(request, "Pool settings saved.")
    return _redirect("/league/settings")


# Payout rules live in app/routers/payouts.py now (the Set Payouts screen at /league/payouts:
# four scopes, dollar-or-percent modes, a pot with an override, and frozen award snapshots).
# The old add/remove-only, float, three-scope routes that used to live here are gone; see
# DECISIONS.md, "Payout system".


@router.post("/join-code")
def rotate_join_code(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    for _ in range(10):
        code = generate_join_code()
        if db.scalar(select(Pool).where(func.upper(Pool.join_code) == code)) is None:
            pool.join_code = code
            db.commit()
            flash(request, f"New join code: {code}. The old code no longer works.")
            return _redirect("/league/members")
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not generate a join code.")


@router.post("/join-code/set")
def set_join_code(
    request: Request,
    join_code: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    code = normalize_join_code(join_code)
    if len(code) < 4:
        flash(request, "A join code needs at least 4 characters.", "error")
        return _redirect("/league/members")
    clash = db.scalar(select(Pool).where(func.upper(Pool.join_code) == code, Pool.id != pool.id))
    if clash is not None:
        flash(request, "Another pool already uses that code.", "error")
        return _redirect("/league/members")
    pool.join_code = code
    db.commit()
    flash(request, f"Join code set to {code}.")
    return _redirect("/league/members")


# Members --------------------------------------------------------------------


def _duplicate_venmo_member_ids(rows: list) -> set[int]:
    """Member ids that share a non-empty member_venmo_handle with at least one other member.

    Case insensitive, whitespace trimmed, so "PatSmith" and " patsmith " still flag each
    other. A surfaced warning only, per the brief ("this does not need to block anything,
    just surface it"): the group banned multiple accounts under one Venmo, but nothing here
    stops a commissioner from saving or acting, it just marks both rows.
    """
    by_handle: dict[str, list[int]] = {}
    for member, _player in rows:
        handle = (member.member_venmo_handle or "").strip().lower()
        if handle:
            by_handle.setdefault(handle, []).append(member.id)
    return {mid for ids in by_handle.values() if len(ids) > 1 for mid in ids}


@router.get("/members")
def members_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    rows = list(
        db.execute(
            select(PoolMember, User)
            .join(User, User.id == PoolMember.user_id)
            .where(PoolMember.pool_id == pool.id)
            .order_by(User.display_name)
        )
    )
    paid_count = sum(1 for member, _player in rows if member.paid_at is not None)
    return render(
        request,
        "admin/members.html",
        {
            "members": list(rows),
            "duplicate_venmo_member_ids": _duplicate_venmo_member_ids(rows),
            "paid_count": paid_count,
            "entry_fee": pool.entry_fee,
        },
        **_base(db, user, pool),
    )


@router.post("/members/invite")
def members_invite_email(
    request: Request,
    emails: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """Commissioner only (Phase 7 remediation, see DECISIONS.md), sends the join link and code
    to one or more addresses, one per line, reusing
    app.routers.leagues._parse_commissioner_emails verbatim rather than a second textarea
    parser. Sits next to the existing "Invite players" copy-and-paste panel on this same page,
    never replaces it: whatever addresses fail (mail off, not configured, rate limited, or the
    provider call itself failing) are named in a flash, and the message above still works to
    copy and send by hand."""
    addresses = _parse_commissioner_emails(emails)
    if not addresses:
        flash(request, "Enter at least one email address, one per line.", "error")
        return _redirect("/league/members")

    link = f"{settings.base_url}/register?code={pool.join_code}"
    subject = f"Join {pool.name} on PickSportPlus"
    body = (
        "Hey,\n\n"
        f"You are invited to join {pool.name} on PickSportPlus, a confidence pick'em pool. "
        f"Join with this link:\n\n{link}\n\n"
        f"Or use join code {pool.join_code} on the register page.\n\n"
        "See you on the leaderboard."
    )
    sent: list[str] = []
    failed: list[str] = []
    for address in addresses:
        try:
            mail.send(
                db,
                to=address,
                subject=subject,
                html=mail.text_to_html(body),
                text=body,
                kind="player_invite",
                actor_key=f"user:{user.id}",
            )
        except mail.MailError as exc:
            failed.append(f"{address} ({exc})")
        else:
            sent.append(address)
    db.commit()

    if sent:
        noun = "address" if len(sent) == 1 else "addresses"
        flash(request, f"Invite emailed to {len(sent)} {noun}.")
    if failed:
        flash(request, "Could not email: " + "; ".join(failed), "error")
        flash(request, "The message above still works. Copy it and send it yourself.", "info")
    return _redirect("/league/members")


@router.post("/members/{member_id}/paid")
def member_paid_toggle(
    request: Request,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    if member.paid_at is None:
        member.paid_at = utcnow()
        member.paid_marked_by_user_id = user.id
        flash(request, "Marked paid.")
    else:
        member.paid_at = None
        member.paid_marked_by_user_id = None
        flash(request, "Marked unpaid.")
    db.commit()
    return _redirect("/league/members")


@router.post("/members/{member_id}/venmo-handle")
def member_venmo_handle_save(
    request: Request,
    member_id: int,
    member_venmo_handle: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """The commissioner's own reconciliation note (never a place to pay, see
    PoolMember.member_venmo_handle's docstring), typed in as payments come in so two members
    sharing one Venmo account (banned: "1 person to pay, no multiple accounts") shows up as a
    warning on this page."""
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    member.member_venmo_handle = member_venmo_handle.strip() or None
    db.commit()
    flash(request, "Venmo handle noted.")
    return _redirect("/league/members")


@router.post("/members/paid/bulk")
def members_paid_bulk(
    request: Request,
    member_ids: list[int] = Form([]),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    now = utcnow()
    count = 0
    for member_id in member_ids:
        member = db.get(PoolMember, member_id)
        if member is None or member.pool_id != pool.id or member.paid_at is not None:
            continue
        member.paid_at = now
        member.paid_marked_by_user_id = user.id
        count += 1
    db.commit()
    if count:
        noun = "member" if count == 1 else "members"
        flash(request, f"Marked {count} {noun} paid.")
    else:
        flash(request, "Nobody selected was still unpaid.", "info")
    return _redirect("/league/members")


@router.post("/members/{member_id}/role")
def member_role(
    request: Request,
    member_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_full_commissioner),
):
    """The site admin's instant, unconfirmed promote/demote power (Post-launch fixes), now
    also reachable by a real full commissioner of their own pool, gated by
    require_full_commissioner so a co-commissioner is refused a 403 either way (it checks
    role_in_pool == "commissioner" exactly, never the broader is_commissioner). A non-admin
    full commissioner may only demote (role="member"): promoting straight to "commissioner"
    stays site-admin-only, since a full commissioner's own promotion path is the confirmed
    co-commissioner invite below, not this instant toggle. Setting role_in_pool through here
    always clears any pending co_commissioner_invited_at, so a role changed by hand never
    leaves a stale invite behind for someone to accept later into a role they no longer make
    sense to be offered."""
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    if role not in ("commissioner", "member"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role.")
    if role == "commissioner" and not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the site admin can promote directly. Invite a co-commissioner instead; "
            "they will need to accept it.",
        )
    member.role_in_pool = role
    member.co_commissioner_invited_at = None
    db.commit()
    flash(request, "Member role updated.")
    return _redirect("/league/members")


@router.post("/members/{member_id}/co-commissioner/invite")
def co_commissioner_invite(
    request: Request,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_full_commissioner),
):
    """A full commissioner inviting a plain member to become a co-commissioner. Unlike
    member_role above, this never changes role_in_pool on its own: it only sets
    co_commissioner_invited_at, and the invited member's own accept (POST
    /league/co-commissioner/accept) is what actually promotes them. See DECISIONS.md,
    Post-launch fixes."""
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    if member.role_in_pool != "member":
        flash(request, "That person is already a commissioner or co-commissioner.", "info")
        return _redirect("/league/members")
    if member.co_commissioner_invited_at is not None:
        flash(request, "There is already a co-commissioner invite pending for them.", "info")
        return _redirect("/league/members")
    member.co_commissioner_invited_at = utcnow()
    db.commit()
    flash(request, "Co-commissioner invite sent. It takes effect once they accept it.")
    return _redirect("/league/members")


@router.post("/members/{member_id}/co-commissioner/cancel")
def co_commissioner_cancel(
    request: Request,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_full_commissioner),
):
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    member.co_commissioner_invited_at = None
    db.commit()
    flash(request, "Co-commissioner invite canceled.")
    return _redirect("/league/members")


@router.post("/co-commissioner/accept")
def co_commissioner_accept(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """The invited member's own confirmation step, not the commissioner who sent the invite
    (that is co_commissioner_invite above). Deliberately not gated by require_commissioner or
    require_full_commissioner: the whole point is that the caller is still a plain member at
    this moment. Acts only on the caller's own PoolMember row in their active pool, never
    anyone else's."""
    member = membership_for(db, user, pool)
    if member is None or member.co_commissioner_invited_at is None:
        flash(request, "There is no pending co-commissioner invite to accept.", "error")
        return _redirect("/picks")
    member.role_in_pool = "co_commissioner"
    member.co_commissioner_invited_at = None
    db.commit()
    flash(request, f"You are now a co-commissioner of {pool.name}.")
    return _redirect("/league")


@router.post("/co-commissioner/decline")
def co_commissioner_decline(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """Clears the pending invite and nothing else: role_in_pool stays "member"."""
    member = membership_for(db, user, pool)
    if member is None or member.co_commissioner_invited_at is None:
        flash(request, "There is no pending co-commissioner invite to decline.", "error")
        return _redirect("/picks")
    member.co_commissioner_invited_at = None
    db.commit()
    flash(request, "Co-commissioner invite declined.")
    return _redirect("/picks")


@router.post("/commissioner-code")
def rotate_commissioner_invite_code_self(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_full_commissioner),
):
    """A full commissioner's self-service view of Pool.commissioner_invite_code (Post-launch
    fixes), the same link and rotation the site admin already manages from /site/leagues
    (app/routers/leagues.py). Gated require_full_commissioner, not require_commissioner: a
    co-commissioner never sees or rotates this link, since sharing it is functionally
    identical to creating a new full commissioner outright, no confirmation step involved.
    Reuses leagues.py's own _fresh_commissioner_invite_code, never a second generator, the
    same collision-avoidance shape rotate_join_code (above, in this same file) already uses."""
    pool.commissioner_invite_code = _fresh_commissioner_invite_code(db, exclude_pool_id=pool.id)
    db.commit()
    flash(request, "New commissioner invite link generated. The old link no longer works.")
    return _redirect("/league/members")


@router.post("/members/{member_id}/remove")
def member_remove(
    request: Request,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    if member.user_id == user.id:
        flash(request, "You cannot remove yourself from the pool.", "error")
        return _redirect("/league/members")
    # Their picks and week entries go with them, which keeps the leaderboard honest.
    db.delete(member)
    db.commit()
    flash(request, "Member removed.")
    return _redirect("/league/members")


# Slate editor ---------------------------------------------------------------


@router.get("/slate")
def slate_page(
    request: Request,
    week: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    row = _week_or_none(db, pool, week)
    weeks = list(
        db.scalars(
            select(Week)
            .where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
            .order_by(Week.week_number.desc())
        )
    )
    on_slate: list[Game] = []
    candidates: list[Game] = []
    editable = False
    pick_count = 0
    reasons: dict[int, str] = {}
    pinned_count = 0
    missing_spread_count = 0
    slate_span_info: dict | None = None

    if row is not None:
        games = list(db.scalars(select(Game).where(Game.week_id == row.id)))
        on_slate = sorted([g for g in games if g.in_slate], key=lambda g: (g.slate_rank or 999))
        candidates = sorted(
            [g for g in games if not g.in_slate],
            key=lambda g: (
                g.closeness if g.closeness is not None else float("inf"),
                g.start_time,
            ),
        )
        editable = ingest.can_resize_slate(db, row)
        pick_count = (
            db.scalar(select(func.count(func.distinct(Pick.user_id))).where(Pick.week_id == row.id))
            or 0
        )
        reasons = {g.id: ingest.slate_reason(g, pool) for g in on_slate}
        pinned_count = sum(1 for g in games if g.pinned and g.status != "void")
        missing_spread_count = sum(1 for g in games if g.spread_home is None and g.status != "void")

        span = ingest.slate_span(db, row)
        if span is not None:
            span_days, earliest, latest = span
            slate_span_info = {
                "span_days": span_days,
                "earliest": earliest,
                "latest": latest,
                "wide": (latest - earliest) > dt.timedelta(hours=48),
            }

    return render(
        request,
        "admin/slate.html",
        {
            "week": row,
            "weeks": weeks,
            "on_slate": on_slate,
            "candidates": candidates,
            "editable": editable,
            "pick_count": pick_count,
            "targets": pool.league_targets,
            "league_labels": LEAGUE_LABELS,
            "warnings": provider_warnings(db),
            "reasons": reasons,
            "pinned_count": pinned_count,
            "missing_spread_count": missing_spread_count,
            "slate_span": slate_span_info,
            # The global switch (Phase 5 remediation), read fresh so the neutral note below
            # always reflects whatever the site admin has it set to right now, never a stale
            # value. No billing/credit language reaches the commissioner: see slate.html.
            "espn_only": get_platform_settings(db).espn_only,
        },
        **_base(db, user, pool),
    )


def _week_for_action(db: Session, pool: Pool, week_id: int) -> Week:
    row = db.get(Week, week_id)
    if row is None or row.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That week is not part of this pool.")
    return row


@router.post("/slate/build")
def slate_build(
    request: Request,
    week_number: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """No publish or no_metered fields anymore (Phase 5 remediation, see DECISIONS.md).
    Publishing is a separate, deliberate action (POST /league/slate/publish, the "Publish
    this week" button lower on this same page); this route always follows pool.auto_publish
    (publish=None, build_slate's own default) exactly as the removed checkbox's own help text
    already promised for its unchecked state. Whether metered providers get called at all is
    no longer a per-build choice either: it is the site admin's global "ESPN only" switch
    (POST /site/providers/espn-only), which ingest.build_slate reads fresh on every call, so
    this route no longer passes allow_metered here at all.

    Phase 6 remediation (see DECISIONS.md): this can legitimately take several seconds to just
    under a minute (ESPN for the schedule, then possibly The Odds API/CFBD for every candidate
    game), and used to give no feedback at all while it ran. Three things changed, all here:
    the response shape now uses _redirect_or_hx so the build form's htmx request (see
    admin/slate.html) gets a real full page navigation instead of a swapped partial;
    ingest.build_slate now refuses a second concurrent build for the same pool week
    (BuildInProgress) instead of letting two runs race; and it now carries a 90 second wall
    clock budget (BuildTimeout) that names whichever provider call was in flight when it ran
    out. Neither new exception changes this route's own response shape, both are flash-and-
    redirect exactly like the pre-existing ValueError branch below."""
    target = f"/league/slate?week={week_number}"
    try:
        report = ingest.build_slate(
            db,
            pool,
            pool.season_year,
            week_number,
        )
    except ingest.BuildInProgress as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect_or_hx(request, target)
    except ingest.BuildTimeout as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect_or_hx(request, target)
    except ValueError as exc:
        # Most likely more games are pinned than the slate total allows (app.slate raises
        # this rather than silently truncating the pins). Reduce the total, or unpin a game,
        # from the slate editor below.
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect_or_hx(request, target)
    db.commit()

    if report.selected and not report.locked_out:
        # A real build, not a no-op (picks already exist so the slate was left alone) or a
        # dead end (nothing came back to select from). Names what was actually built, not
        # ingest.IngestReport.summary()'s own longer sentence, which several other callers
        # (app/cli.py, app/services/demo.py) still rely on verbatim and this phase leaves
        # untouched. See DECISIONS.md, Phase 6, for why this is a new sentence rather than an
        # edit to summary() itself.
        flash(
            request,
            f"Week {report.week_number} built. {report.selected} games, "
            f"{report.per_league.get('nfl', 0)} NFL and {report.per_league.get('ncaaf', 0)} "
            f"college, {report.missing_spread} missing a line.",
            "ok",
        )
    else:
        flash(request, report.summary(), "ok" if report.selected else "info")
    for note in report.notes:
        flash(request, note, "info")
    for warning in report.warnings:
        flash(request, warning, "error")
    return _redirect_or_hx(request, target)


@router.post("/slate/publish")
def slate_publish(
    request: Request,
    week_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    row = _week_for_action(db, pool, week_id)
    if row.status != "draft":
        flash(request, f"Week {row.week_number} is already {row.status}.", "info")
    else:
        try:
            notify_warnings = ingest.publish_week(db, row)
        except ingest.SlateSpanTooWide as exc:
            db.rollback()
            flash(request, str(exc), "error")
        else:
            db.commit()
            flash(request, f"Week {row.week_number} is open for picks.")
            for warning in notify_warnings:
                flash(request, warning, "error")
    return _redirect(f"/league/slate?week={row.week_number}")


@router.post("/slate/game")
def slate_game_action(
    request: Request,
    week_id: int = Form(...),
    game_id: int = Form(...),
    action: str = Form(...),
    swap_with: int | None = Form(None),
    spread: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    row = _week_for_action(db, pool, week_id)
    try:
        if action == "add":
            ingest.add_to_slate(db, row, game_id)
            flash(request, "Game added to the slate.")
        elif action == "remove":
            ingest.remove_from_slate(db, row, game_id)
            flash(request, "Game removed from the slate.")
        elif action == "swap":
            if swap_with is None:
                raise ValueError("Choose a game to swap in.")
            ingest.swap_slate_game(db, row, game_id, swap_with)
            flash(request, "Games swapped.")
        elif action == "void":
            ingest.set_void(db, row, game_id, True)
            flash(request, "Game voided. Nobody scores it and it leaves the possible count.")
        elif action == "unvoid":
            ingest.set_void(db, row, game_id, False)
            flash(request, "Game restored.")
        elif action == "spread":
            value = spread.strip()
            ingest.set_manual_spread(db, row, game_id, float(value) if value else None)
            flash(request, "Line set by hand. No feed will overwrite it.")
        elif action == "pin":
            ingest.set_pinned(db, row, game_id, True)
            flash(request, "Game pinned. It will always be on the slate the next time it is built.")
        elif action == "unpin":
            ingest.set_pinned(db, row, game_id, False)
            flash(request, "Game unpinned.")
        else:
            raise ValueError("Unknown action.")
        db.commit()
    except ingest.SlateLocked as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _redirect(f"/league/slate?week={row.week_number}")


@router.post("/slate/lock")
def slate_lock(
    request: Request,
    week_id: int = Form(...),
    lock_at_local: str = Form(""),
    clear: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    row = _week_for_action(db, pool, week_id)
    if clear:
        row.lock_at_override = False
        ingest.recompute_lock(db, row)
        db.commit()
        flash(request, "Lock time is back to the first kickoff.")
        return _redirect(f"/league/slate?week={row.week_number}")

    if not lock_at_local:
        flash(request, "Enter a lock time.", "error")
        return _redirect(f"/league/slate?week={row.week_number}")
    try:
        naive = dt.datetime.fromisoformat(lock_at_local)
    except ValueError:
        flash(request, "That is not a valid date and time.", "error")
        return _redirect(f"/league/slate?week={row.week_number}")

    local = naive.replace(tzinfo=get_zone(pool.timezone))
    row.lock_at = local.astimezone(dt.UTC)
    row.lock_at_override = True
    db.commit()
    flash(request, "Lock time set.")
    return _redirect(f"/league/slate?week={row.week_number}")


# Test week -------------------------------------------------------------------


@router.post("/test-week/create")
def test_week_create(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """Build (or rebuild) the pool's one test week from whatever is live right now, NFL
    preseason and college week 0 included (Phase 3, preseason and test week support). Reuses
    the ordinary slate-build machinery end to end, ingest.build_slate(is_test_week=True) is
    the only thing that differs from a real week's build. Auto-published (publish=True)
    rather than left as a draft: the whole point of a test week is letting the group see picks
    and scoring work end to end with as few extra clicks as possible, and it is explicitly
    quarantined from every season-wide and money-related computation, so there is no real-week
    caution to preserve by holding it back for a manual publish."""
    try:
        report = ingest.build_slate(
            db,
            pool,
            pool.season_year,
            ingest.TEST_WEEK_NUMBER,
            publish=True,
            is_test_week=True,
        )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect("/league/slate")
    db.commit()
    flash(request, report.summary(), "ok" if report.selected else "info")
    for note in report.notes:
        flash(request, note, "info")
    for warning in report.warnings:
        flash(request, warning, "error")
    return _redirect(f"/league/slate?week={ingest.TEST_WEEK_NUMBER}")


@router.post("/test-week/{week_id}/delete")
def test_week_delete(
    request: Request,
    week_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """Delete a test week outright: its Game, Pick and WeekEntry rows all go with it through
    the existing cascade="all, delete-orphan" relationships on Week (app/models.py). A real
    week never allows this, so the check below is the only thing standing between this route
    and deleting a real week's history; it refuses rather than silently no-opping so a stale
    week_id (a real week, or one from another pool) is never quietly ignored."""
    row = _week_for_action(db, pool, week_id)
    if not row.is_test_week:
        flash(request, "Only a test week can be deleted this way.", "error")
        return _redirect("/league/slate")
    db.delete(row)
    db.commit()
    flash(request, "Test week deleted.")
    return _redirect("/league/slate")


# Manual job triggers --------------------------------------------------------


@router.post("/run/results")
def run_results(
    request: Request,
    week_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    from app.services.results import fetch_results, score_week_for_pool

    row = _week_for_action(db, pool, week_id)
    results = fetch_results(db, pool, row)
    score = score_week_for_pool(db, pool, row)
    db.commit()
    flash(request, results.summary())
    flash(request, score.summary())
    for warning in results.warnings:
        flash(request, warning, "error")
    return _redirect(f"/league/slate?week={row.week_number}")


__all__ = ["router"]
