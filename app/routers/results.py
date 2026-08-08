"""Results: every game outcome for a week, and every player's picks once the week has locked."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_active_pool, is_commissioner, require_user
from app.db import get_db
from app.models import Game, Pick, Pool, PoolMember, User, Week, WeekEntry
from app.routers.picks import week_is_locked
from app.scoring import GameOutcome, PickInput, score_pick
from app.templating import render

router = APIRouter(tags=["results"])


@dataclass
class PlayerPick:
    picked_team: str | None
    confidence: int | None
    earned: int
    state: str  # correct, wrong, void, pending, missing


@dataclass
class PlayerColumn:
    user_id: int
    display_name: str
    points: int
    correct: int
    is_winner: bool
    submitted: bool
    did_not_submit: bool
    picks: dict[int, PlayerPick]


def _week_or_404(db: Session, pool: Pool, week_number: int | None) -> Week | None:
    query = select(Week).where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
    if week_number is not None:
        week = db.scalar(query.where(Week.week_number == week_number))
        if week is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Week {week_number} does not exist.")
        return week
    return db.scalar(
        query.where(Week.status.in_(("open", "locked", "scored"))).order_by(Week.week_number.desc())
    )


@router.get("/results")
def results_page(
    request: Request,
    week: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    row = _week_or_404(db, pool, week)
    weeks = list(
        db.scalars(
            select(Week)
            .where(Week.pool_id == pool.id, Week.season_year == pool.season_year)
            .where(Week.status.in_(("open", "locked", "scored")))
            .order_by(Week.week_number.desc())
        )
    )

    games: list[Game] = []
    columns: list[PlayerColumn] = []
    revealed = False

    if row is not None:
        games = list(
            db.scalars(
                select(Game)
                .where(Game.week_id == row.id, Game.in_slate.is_(True))
                .order_by(Game.slate_rank)
            )
        )
        # Picks stay private until the week locks, then everyone sees everything.
        revealed = week_is_locked(row)
        if revealed:
            columns = _build_columns(db, pool, row, games)

    return render(
        request,
        "results.html",
        {
            "week": row,
            "weeks": weeks,
            "games": games,
            "columns": columns,
            "revealed": revealed,
            "n": len(games),
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="results",
    )


def _build_columns(db: Session, pool: Pool, week: Week, games: list[Game]) -> list[PlayerColumn]:
    members = list(
        db.scalars(
            select(User)
            .join(PoolMember, PoolMember.user_id == User.id)
            .where(PoolMember.pool_id == pool.id)
            .order_by(User.display_name)
        )
    )
    entries = {
        e.user_id: e for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id))
    }
    picks_by_user: dict[int, dict[int, Pick]] = {}
    for pick in db.scalars(select(Pick).where(Pick.week_id == week.id)):
        picks_by_user.setdefault(pick.user_id, {})[pick.game_id] = pick

    columns: list[PlayerColumn] = []
    for member in members:
        entry = entries.get(member.id)
        user_picks = picks_by_user.get(member.id, {})
        cells: dict[int, PlayerPick] = {}
        for game in games:
            pick = user_picks.get(game.id)
            if pick is None:
                cells[game.id] = PlayerPick(None, None, 0, "missing")
                continue
            if game.status == "void" or game.winner == "tie":
                state, earned = "void", 0
            elif game.status != "final" or game.winner is None:
                state, earned = "pending", 0
            else:
                # "correct" and "wrong" describe whether the pick matched the winner, the
                # same in both scoring modes. earned is what the pick actually scored,
                # which is mode dependent: score_pick returns 0 for a correct pick and the
                # staked points against the player for a wrong one under "inverse".
                state = "correct" if pick.picked_team == game.winner else "wrong"
                earned = score_pick(
                    PickInput(
                        game_id=game.id, picked_team=pick.picked_team, confidence=pick.confidence
                    ),
                    GameOutcome(game_id=game.id, status=game.status, winner=game.winner),
                    mode=pool.scoring_mode,
                )
            cells[game.id] = PlayerPick(pick.picked_team, pick.confidence, earned, state)

        columns.append(
            PlayerColumn(
                user_id=member.id,
                display_name=member.display_name,
                points=entry.points if entry else 0,
                correct=entry.correct if entry else 0,
                is_winner=bool(entry and entry.is_winner),
                submitted=bool(entry and entry.submitted_at),
                did_not_submit=bool(entry.did_not_submit) if entry else True,
                picks=cells,
            )
        )

    sign = 1 if pool.scoring_mode == "inverse" else -1
    columns.sort(key=lambda c: (sign * c.points, -c.correct, c.display_name.lower()))
    return columns


__all__ = ["router"]
