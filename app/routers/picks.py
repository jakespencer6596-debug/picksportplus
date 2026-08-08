"""The This Week page: choose winners, rank confidence, save before lock."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import flash, get_active_pool, is_commissioner, membership_for, require_user
from app.db import get_db
from app.models import Game, Pick, Pool, PoolMember, User, Week, WeekEntry
from app.scoring import PickInput, validate_picks
from app.templating import render

router = APIRouter(tags=["picks"])


def payment_gate_blocks(member: PoolMember | None, pool: Pool) -> bool:
    """True when the Venmo entry gate (Phase 7) still stands between this member and a pick.

    Off entirely when the pool does not require payment to pick. Otherwise blocks anyone with
    no membership row at all (should not happen for a request that reached get_active_pool,
    handled defensively) and anyone whose PoolMember.paid_at is still null. Shared by
    picks_page (to render the blocking panel) and _save_picks/_lock_picks (the actual
    server side enforcement, exactly as authoritative as validate_picks): both call this one
    function so the page and the gate can never quietly disagree about who is blocked.
    """
    if not pool.payment_required_to_pick:
        return False
    return member is None or member.paid_at is None


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
    entry: WeekEntry | None = None
    if week is not None:
        games = slate_games(db, week)
        picks_by_game = {
            p.game_id: p
            for p in db.scalars(
                select(Pick).where(Pick.user_id == user.id, Pick.week_id == week.id)
            )
        }
        entry = db.scalar(
            select(WeekEntry).where(WeekEntry.user_id == user.id, WeekEntry.week_id == week.id)
        )

    # Saved picks drive the row order, highest confidence at the top, so returning to the page
    # shows the ranking the player left behind. Otherwise fall back to slate rank, which puts
    # the closest game first as a reasonable starting point. A "complete" entry means exactly
    # picks_required picks are in, not that every slate game has one: a picked game is legal to
    # leave off the slate now (Phase 3), so picks_by_game can be smaller than games on purpose.
    if picks_by_game and len(picks_by_game) == pool.picks_required:
        games.sort(
            key=lambda g: (
                0 if g.id in picks_by_game else 1,
                -picks_by_game[g.id].confidence if g.id in picks_by_game else 0,
            )
        )

    # The pool wide time lock (week.lock_at, enforced server side no matter what) always
    # wins once it hits. player_locked is the separate, player initiated lock (Phase 4):
    # true only while the player has locked their own picks AND the real lock has not yet
    # arrived. The moment `locked` flips True the template's time locked branch takes over
    # regardless of locked_at, so a player lock never outlives or overrides the real one.
    locked = week is not None and week_is_locked(week)
    player_locked = not locked and entry is not None and entry.locked_at is not None
    next_week = _next_unpublished_week(db, pool) if week is None else None

    # Venmo entry gate (Phase 7): only relevant to the still-open, still-editable state. A
    # week that is already time locked or already player locked shows exactly what it showed
    # before this phase, paid or not, since there is nothing left to create or lock there;
    # see payment_gate_blocks's own docstring and _save_picks/_lock_picks for the matching
    # server side enforcement.
    member = membership_for(db, user, pool)
    payment_blocked = not locked and not player_locked and payment_gate_blocks(member, pool)

    return render(
        request,
        "picks.html",
        {
            "week": week,
            "games": games,
            "picks_by_game": picks_by_game,
            "locked": locked,
            "player_locked": player_locked,
            "payment_blocked": payment_blocked,
            "locked_at": entry.locked_at if entry else None,
            # n is the target picks a player must submit, not the slate size (games can be
            # bigger, for example 20 games with 15 required). Never hard coded, always the
            # pool's own commissioner-set value.
            "n": pool.picks_required,
            "submitted_count": len(picks_by_game),
            "has_full_entry": len(picks_by_game) == pool.picks_required and bool(games),
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

    Nothing from the browser is trusted. The submission must contain exactly
    pool.picks_required picks, confidence values that are a permutation of 1 to
    picks_required, and it must arrive before lock_at. picks_required is a commissioner
    setting on the pool, never a hard coded number, and it can be smaller than the slate
    (Phase 3: 15 picks out of a 20 game slate is the pool's real rule).
    """
    form = await request.form()
    raw = {key: str(value) for key, value in form.items()}
    return await run_in_threadpool(_save_picks, request, db, user, pool, raw)


def _parse_submission(games: list[Game], raw: dict[str, str]) -> tuple[list[PickInput], list[str]]:
    """Parse the winner-{id} / confidence-{id} fields the picks form posts.

    Shared by /picks and /picks/lock, so there is exactly one reading of the raw form data.
    A game with neither field present is skipped: an unpicked slate game is legal (Phase 3),
    not an incomplete row. A game with one field present but not the other is NOT skipped,
    so it reaches validate_picks and comes back as a real, readable error rather than being
    silently dropped.
    """
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
    return submitted, malformed


def _upsert_picks(
    db: Session, user: User, pool: Pool, week: Week, submitted: list[PickInput], now: dt.datetime
) -> WeekEntry:
    """Write the winners and confidence to Pick rows and touch WeekEntry.submitted_at.

    The one write path shared by /picks and /picks/lock. Never touches WeekEntry.locked_at:
    only the caller in /picks/lock does that, after this returns.
    """
    existing = {
        p.game_id: p
        for p in db.scalars(select(Pick).where(Pick.user_id == user.id, Pick.week_id == week.id))
    }
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
        entry = WeekEntry(user_id=user.id, pool_id=pool.id, week_id=week.id, submitted_at=now)
        db.add(entry)
    elif entry.submitted_at is None:
        entry.submitted_at = now
    return entry


def _save_picks(request: Request, db: Session, user: User, pool: Pool, raw: dict[str, str]):
    week = current_week(db, pool)
    if week is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no open week right now.")
    if week_is_locked(week):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This week is locked. Picks closed at the first kickoff.",
        )
    if payment_gate_blocks(membership_for(db, user, pool), pool):
        # Exactly as authoritative as validate_picks below: a hand crafted POST from someone
        # who never paid, or whose client is simply stale, must be refused here too, not only
        # by the disabled controls picks_page renders. See payment_gate_blocks's docstring.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Pay your entry fee before picks are accepted. See the panel on the picks page "
            "for how, then ask your commissioner to mark you paid.",
        )

    games = slate_games(db, week)
    slate_ids = [g.id for g in games]
    submitted, malformed = _parse_submission(games, raw)

    errors = malformed + validate_picks(submitted, slate_ids, pool.picks_required)
    if errors:
        return render(
            request,
            "components/pick_status.html",
            {"errors": errors, "saved": False, "n": pool.picks_required},
            current_user=user,
            pool=pool,
            status_code=400,
        )

    now = dt.datetime.now(dt.UTC)
    _upsert_picks(db, user, pool, week, submitted, now)
    db.commit()

    if request.headers.get("HX-Request") == "true":
        return render(
            request,
            "components/pick_status.html",
            {"saved": True, "errors": [], "n": pool.picks_required, "saved_at": now},
            current_user=user,
            pool=pool,
        )
    flash(request, "Your picks are in.")
    return RedirectResponse("/picks", status_code=303)


@router.post("/picks/lock")
async def picks_lock(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """Lock exactly picks_required picks in, a player initiated step distinct from Save.

    Runs the exact same server side validation Save does (validate_picks, never weakened
    or duplicated) before writing anything or touching locked_at, so a hand crafted POST to
    this route is exactly as safe as one to /picks.
    """
    form = await request.form()
    raw = {key: str(value) for key, value in form.items()}
    return await run_in_threadpool(_lock_picks, request, db, user, pool, raw)


def _lock_picks(request: Request, db: Session, user: User, pool: Pool, raw: dict[str, str]):
    week = current_week(db, pool)
    if week is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no open week right now.")
    if week_is_locked(week):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This week is locked. Picks closed at the first kickoff.",
        )
    if payment_gate_blocks(membership_for(db, user, pool), pool):
        # The exact same gate and message _save_picks enforces: locking is just as much
        # "creating a pick" as saving is, so it gets the identical, authoritative check.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Pay your entry fee before picks are accepted. See the panel on the picks page "
            "for how, then ask your commissioner to mark you paid.",
        )

    games = slate_games(db, week)
    slate_ids = [g.id for g in games]
    submitted, malformed = _parse_submission(games, raw)

    errors = malformed + validate_picks(submitted, slate_ids, pool.picks_required)
    if errors:
        # Same partial, same shape as a rejected Save: nothing is written, locked_at is
        # never touched, and the player sees exactly the same messages Save would give.
        return render(
            request,
            "components/pick_status.html",
            {"errors": errors, "saved": False, "n": pool.picks_required},
            current_user=user,
            pool=pool,
            status_code=400,
        )

    now = dt.datetime.now(dt.UTC)
    entry = _upsert_picks(db, user, pool, week, submitted, now)
    entry.locked_at = now
    db.commit()

    if request.headers.get("HX-Request") == "true":
        # The locked view is a genuinely different page state (Section 8's confirmation
        # branch, read only until unlocked), not a small swap into the save bar, so a full
        # reload of /picks is simpler and more honest than trying to fake that state with a
        # partial. htmx follows HX-Redirect as a real navigation.
        return Response(status_code=200, headers={"HX-Redirect": "/picks"})
    flash(request, f"Picks locked for {week.label}.")
    return RedirectResponse("/picks", status_code=303)


@router.post("/picks/unlock")
async def picks_unlock(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """Clear a player's own lock. Refused once the week's real lock_at has passed: the pool
    wide time lock always wins and a player can never claw back editing time from it."""
    return await run_in_threadpool(_unlock_picks, request, db, user, pool)


def _unlock_picks(request: Request, db: Session, user: User, pool: Pool):
    week = current_week(db, pool)
    if week is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no open week right now.")
    if week_is_locked(week):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This week is locked. You can no longer unlock your picks.",
        )

    entry = db.scalar(
        select(WeekEntry).where(WeekEntry.user_id == user.id, WeekEntry.week_id == week.id)
    )
    if entry is not None and entry.locked_at is not None:
        entry.locked_at = None
        db.commit()

    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": "/picks"})
    flash(request, "Picks unlocked. Make your changes and lock again before kickoff.")
    return RedirectResponse("/picks", status_code=303)


__all__ = ["router", "week_is_locked", "current_week", "slate_games", "payment_gate_blocks"]
