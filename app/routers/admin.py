"""Commissioner tools: pool settings, the slate editor, members, and manual job triggers.

Everything here sits behind require_commissioner. A regular player cannot reach any of it.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    Pool,
    flash,
    generate_join_code,
    normalize_join_code,
    require_commissioner,
    require_user,
)
from app.config import Settings, settings
from app.db import get_db
from app.models import Game, Pick, PoolMember, User, Week
from app.providers.http import provider_warnings, usage_report
from app.services import ingest
from app.templating import get_zone, render

router = APIRouter(prefix="/admin", tags=["admin"])

LEAGUE_LABELS = {"nfl": "NFL", "ncaaf": "College"}


def _redirect(target: str = "/admin") -> RedirectResponse:
    return RedirectResponse(target, status_code=303)


def _week_or_none(db: Session, pool: Pool, week_number: int | None) -> Week | None:
    query = select(Week).where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
    if week_number is not None:
        return db.scalar(query.where(Week.week_number == week_number))
    return db.scalar(
        query.where(Week.status.in_(("draft", "open", "locked"))).order_by(Week.week_number.desc())
    ) or db.scalar(query.order_by(Week.week_number.desc()))


def _base(db: Session, user: User, pool: Pool) -> dict:
    return {"current_user": user, "pool": pool, "is_commissioner": True, "active_nav": "admin"}


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
            "usage": usage_report(db),
            "warnings": provider_warnings(db),
        },
        **_base(db, user, pool),
    )


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
    auto_publish: str = Form(""),
    open_registration: str = Form(""),
    sports_nfl: str = Form(""),
    sports_ncaaf: str = Form(""),
    week1_anchor_date: str = Form(""),
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
        anchor_date = None

    if errors:
        for message in errors:
            flash(request, message, "error")
        return _redirect("/admin/settings")

    pool.name = name
    pool.season_year = season_year
    pool.timezone = timezone
    pool.num_games_per_week = num_games_per_week
    pool.target_nfl = target_nfl
    pool.target_ncaaf = target_ncaaf
    pool.sports = sports
    pool.auto_publish = bool(auto_publish)
    pool.open_registration = bool(open_registration)
    pool.week1_anchor_date = anchor_date
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
    return _redirect("/admin/settings")


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
            return _redirect("/admin/members")
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
        return _redirect("/admin/members")
    clash = db.scalar(select(Pool).where(func.upper(Pool.join_code) == code, Pool.id != pool.id))
    if clash is not None:
        flash(request, "Another pool already uses that code.", "error")
        return _redirect("/admin/members")
    pool.join_code = code
    db.commit()
    flash(request, f"Join code set to {code}.")
    return _redirect("/admin/members")


# Members --------------------------------------------------------------------


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
    return render(
        request,
        "admin/members.html",
        {"members": list(rows)},
        **_base(db, user, pool),
    )


@router.post("/members/{member_id}/role")
def member_role(
    request: Request,
    member_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    member = db.get(PoolMember, member_id)
    if member is None or member.pool_id != pool.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That member is not in this pool.")
    if role not in ("commissioner", "member"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role.")
    if role == "member" and member.user_id == user.id and not user.is_admin:
        flash(request, "You cannot remove your own commissioner role.", "error")
        return _redirect("/admin/members")
    member.role_in_pool = role
    db.commit()
    flash(request, "Member role updated.")
    return _redirect("/admin/members")


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
        return _redirect("/admin/members")
    # Their picks and week entries go with them, which keeps the leaderboard honest.
    db.delete(member)
    db.commit()
    flash(request, "Member removed.")
    return _redirect("/admin/members")


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
    publish: str = Form(""),
    no_metered: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    report = ingest.build_slate(
        db,
        pool,
        pool.season_year,
        week_number,
        allow_metered=not bool(no_metered),
        publish=True if publish else None,
    )
    db.commit()
    flash(request, report.summary(), "ok" if report.selected else "info")
    for note in report.notes:
        flash(request, note, "info")
    for warning in report.warnings:
        flash(request, warning, "error")
    return _redirect(f"/admin/slate?week={week_number}")


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
        ingest.publish_week(db, row)
        db.commit()
        flash(request, f"Week {row.week_number} is open for picks.")
    return _redirect(f"/admin/slate?week={row.week_number}")


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
        else:
            raise ValueError("Unknown action.")
        db.commit()
    except ingest.SlateLocked as exc:
        db.rollback()
        flash(request, str(exc), "error")
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return _redirect(f"/admin/slate?week={row.week_number}")


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
        return _redirect(f"/admin/slate?week={row.week_number}")

    if not lock_at_local:
        flash(request, "Enter a lock time.", "error")
        return _redirect(f"/admin/slate?week={row.week_number}")
    try:
        naive = dt.datetime.fromisoformat(lock_at_local)
    except ValueError:
        flash(request, "That is not a valid date and time.", "error")
        return _redirect(f"/admin/slate?week={row.week_number}")

    local = naive.replace(tzinfo=get_zone(pool.timezone))
    row.lock_at = local.astimezone(dt.UTC)
    row.lock_at_override = True
    db.commit()
    flash(request, "Lock time set.")
    return _redirect(f"/admin/slate?week={row.week_number}")


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
    return _redirect(f"/admin/slate?week={row.week_number}")


__all__ = ["router"]
