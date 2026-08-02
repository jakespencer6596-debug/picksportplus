"""The This Week page: choose winners, rank confidence, save before lock."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import flash, get_active_pool, is_commissioner, require_user
from app.db import get_db
from app.models import Game, Pick, Pool, User, Week, WeekEntry
from app.scoring import PickInput, validate_picks
from app.templating import render

router = APIRouter(tags=["picks"])


def current_week(db: Session, pool: Pool) -> Week | None:
    """The week the player should be looking at.

    Prefer an open week, then the most recent locked or scored week, so the page is never
    blank once a season is under way.
    """
    week = db.scalar(
        select(Week)
        .where(Week.pool_id == pool.id, Week.season_year == pool.season_year, Week.status == "open")
        .order_by(Week.week_number.desc())
    )
    if week:
        return week
    return db.scalar(
        select(Week)
        .where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
        .where(Week.status.in_(("locked", "scored")))
        .order_by(Week.week_number.desc())
    )


def week_is_locked(week: Week, now: dt.datetime | None = None) -> bool:
    """Lock is enforced by the clock, not by the job that was supposed to flip the status.

    A delayed cron must never hand anyone extra time to pick.
    """
    if week.status in ("locked", "scored"):
        return True
    if week.lock_at is None:
        return False
    lock_at = week.lock_at
    if lock_at.tzinfo is None:
        lock_at = lock_at.replace(tzinfo=dt.UTC)
    return (now or dt.datetime.now(dt.UTC)) >= lock_at


def slate_games(db: Session, week: Week) -> list[Game]:
    return list(
        db.scalars(
            select(Game)
            .where(Game.week_id == week.id, Game.in_slate.is_(True))
            .order_by(Game.slate_rank)
        )
    )


@router.get("/picks")
def picks_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    week = current_week(db, pool)
    games: list[Game] = []
    picks_by_game: dict[int, Pick] = {}
    if week is not None:
        games = slate_games(db, week)
        picks_by_game = {
            p.game_id: p
            for p in db.scalars(
                select(Pick).where(Pick.user_id == user.id, Pick.week_id == week.id)
            )
        }

    # Saved picks drive the row order, highest confidence at the top, so returning to the page
    # shows the ranking the player left behind. Otherwise fall back to slate rank, which puts
    # the closest game first as a reasonable starting point.
    if picks_by_game and len(picks_by_game) == len(games):
        games.sort(key=lambda g: -picks_by_game[g.id].confidence)

    locked = week is not None and week_is_locked(week)
    next_week = _next_unpublished_week(db, pool) if week is None else None

    return render(
        request,
        "picks.html",
        {
            "week": week,
            "games": games,
            "picks_by_game": picks_by_game,
            "locked": locked,
            "n": len(games),
            "submitted_count": len(picks_by_game),
            "has_full_entry": len(picks_by_game) == len(games) and bool(games),
            "next_week": next_week,
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="picks",
    )


def _next_unpublished_week(db: Session, pool: Pool) -> Week | None:
    return db.scalar(
        select(Week)
        .where(
            Week.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.status == "draft",
        )
        .order_by(Week.week_number)
    )


@router.post("/picks")
async def picks_save(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """Save winners and confidence.

    Nothing from the browser is trusted. The submission must contain exactly one winner per
    slate game, confidence values that are a permutation of 1 to N, and it must arrive before
    lock_at. N is the real slate size for this week, never a hard coded number.
    """
    form = await request.form()
    raw = {key: str(value) for key, value in form.items()}
    return await run_in_threadpool(_save_picks, request, db, user, pool, raw)


def _save_picks(request: Request, db: Session, user: User, pool: Pool, raw: dict[str, str]):
    week = current_week(db, pool)
    if week is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no open week right now.")
    if week_is_locked(week):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This week is locked. Picks closed at the first kickoff.",
        )

    games = slate_games(db, week)
    slate_ids = [g.id for g in games]

    submitted: list[PickInput] = []
    malformed: list[str] = []
    for game in games:
        side = (raw.get(f"winner-{game.id}") or "").strip()
        raw_conf = (raw.get(f"confidence-{game.id}") or "").strip()
        if not side and not raw_conf:
            continue
        try:
            confidence = int(raw_conf)
        except ValueError:
            malformed.append(f"{game.away_abbr} at {game.home_abbr} is missing a confidence value.")
            continue
        submitted.append(PickInput(game_id=game.id, picked_team=side, confidence=confidence))

    errors = malformed + validate_picks(submitted, slate_ids)
    if errors:
        return render(
            request,
            "components/pick_status.html",
            {"errors": errors, "saved": False, "n": len(games)},
            current_user=user,
            pool=pool,
            status_code=400,
        )

    existing = {
        p.game_id: p
        for p in db.scalars(select(Pick).where(Pick.user_id == user.id, Pick.week_id == week.id))
    }
    now = dt.datetime.now(dt.UTC)
    for item in submitted:
        row = existing.get(item.game_id)
        if row is None:
            db.add(
                Pick(
                    user_id=user.id,
                    pool_id=pool.id,
                    week_id=week.id,
                    game_id=item.game_id,
                    picked_team=item.picked_team,
                    confidence=item.confidence,
                )
            )
        else:
            row.picked_team = item.picked_team
            row.confidence = item.confidence
            row.updated_at = now

    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.user_id == user.id, WeekEntry.week_id == week.id)
    )
    if entry is None:
        db.add(
            WeekEntry(
                user_id=user.id,
                pool_id=pool.id,
                week_id=week.id,
                submitted_at=now,
            )
        )
    elif entry.submitted_at is None:
        entry.submitted_at = now
    db.commit()

    if request.headers.get("HX-Request") == "true":
        return render(
            request,
            "components/pick_status.html",
            {"saved": True, "errors": [], "n": len(games), "saved_at": now},
            current_user=user,
            pool=pool,
        )
    flash(request, "Your picks are in.")
    return RedirectResponse("/picks", status_code=303)


__all__ = ["router", "week_is_locked", "current_week", "slate_games"]
