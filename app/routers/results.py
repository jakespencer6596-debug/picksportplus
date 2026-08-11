"""Results: every game outcome for a week, that week's leaderboard, every player's picks
once the week has locked, and the scenarios panel (Phase 8: placement odds, leverage, and
build-your-own-scenario standings). Season standings live on their own page, see
app/routers/leaderboard.py."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    get_active_pool,
    has_pending_co_commissioner_invite,
    is_commissioner,
    require_user,
)
from app.db import get_db
from app.models import Game, Pick, Pool, PoolMember, User, Week, WeekEntry
from app.routers.picks import week_is_locked
from app.scenarios import PlayerScenarioOutlook, RepresentativeScenario
from app.scoring import GameOutcome, PickInput, score_pick
from app.services import scenarios as scenario_service
from app.services.standings import weekly_leaderboard
from app.templating import render

router = APIRouter(tags=["results"])

# How close to 50/50 a game's leverage share has to be, on either side, before the panel
# calls it a game that "does not matter" to the player rather than naming a team they need.
# The brief's own example ("Detroit vs Chicago does not matter to you") is a plain reading
# of a share near even; 40..60 percent is a generous band for "near even" without being so
# wide that a real, if modest, lean gets swallowed into "does not matter".
_LEVERAGE_INDIFFERENCE_BAND = 0.10


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
    picks: dict[int, PlayerPick]  # keyed by game_id, the game-major view
    # keyed by confidence value (1..pool.picks_required), the player-major view. Built from
    # this player's own Pick rows, not by scanning every slate game, since a player's
    # confidence values only exist for the games they actually picked.
    by_confidence: dict[int, tuple[Game, PlayerPick]] = field(default_factory=dict)


@dataclass
class LeverageLine:
    """One remaining game's leverage, in the plain sentence form the brief asks for."""

    game: Game
    home_pct: float
    away_pct: float
    matters: bool
    sentence: str


@dataclass
class RepresentativeLine:
    """One representative scenario, as a readable list of "TEAM over TEAM" picks."""

    weight: float
    picks: list[str]  # "GB over CHI", one per remaining game in that scenario


def _leverage_lines(
    outlook: PlayerScenarioOutlook, games_by_id: dict[int, Game]
) -> list[LeverageLine]:
    """Turn a leverage table into the plain sentences the brief's own examples use:
    "You need Green Bay in 94 percent of your winning scenarios," or, for a game near
    even within the player's own winning scenarios, "Detroit vs Chicago does not matter
    to you." Ordered by how decisive the game is, most decisive first, so the sentences
    that matter most read first.
    """
    lines: list[LeverageLine] = []
    for game_id, shares in outlook.leverage.items():
        game = games_by_id.get(game_id)
        if game is None:
            continue
        home_pct, away_pct = shares["home"], shares["away"]
        matters = abs(home_pct - 0.5) >= _LEVERAGE_INDIFFERENCE_BAND
        if not matters:
            sentence = f"{game.away_abbr} vs {game.home_abbr} does not matter to you."
        elif home_pct > away_pct:
            sentence = f"You need {game.home_abbr} in {round(home_pct * 100)} percent of your winning scenarios."
        else:
            sentence = f"You need {game.away_abbr} in {round(away_pct * 100)} percent of your winning scenarios."
        lines.append(
            LeverageLine(
                game=game, home_pct=home_pct, away_pct=away_pct, matters=matters, sentence=sentence
            )
        )
    lines.sort(key=lambda line: -abs(line.home_pct - 0.5))
    return lines


def _representative_lines(
    reps: list[RepresentativeScenario], games_by_id: dict[int, Game]
) -> list[RepresentativeLine]:
    lines: list[RepresentativeLine] = []
    for rep in reps:
        picks: list[str] = []
        for game_id, side in rep.assignment:
            game = games_by_id.get(game_id)
            if game is None:
                continue
            winner_abbr = game.home_abbr if side == "home" else game.away_abbr
            loser_abbr = game.away_abbr if side == "home" else game.home_abbr
            picks.append(f"{winner_abbr} over {loser_abbr}")
        lines.append(RepresentativeLine(weight=rep.weight, picks=picks))
    return lines


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
    model: str = "even",
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

    # The probability model toggle (spec: a per user VIEW preference, never a pool setting
    # or a database column). An unrecognised value falls back to "even" rather than 400ing,
    # since this only ever arrives from this page's own links.
    if model not in ("even", "moneyline"):
        model = "even"

    games: list[Game] = []
    columns: list[PlayerColumn] = []
    revealed = False
    weekly = []
    payouts_by_user: dict[int, float] = {}
    payout_rules_exist = False
    payout_scope: str | None = None
    scenario_panel = None
    leverage_lines: list[LeverageLine] = []
    representative_lines: list[RepresentativeLine] = []
    remaining_games_by_id: dict[int, Game] = {}

    if row is not None:
        games = list(
            db.scalars(
                select(Game)
                .where(Game.week_id == row.id, Game.in_slate.is_(True))
                .order_by(Game.slate_rank)
            )
        )
        # Picks stay private until the week locks, then everyone sees everything. The
        # weekly leaderboard follows the same rule, not just the pick grid: an entry
        # existing at all is a signal someone already submitted, so it stays hidden too.
        # The scenarios panel below follows suit: its own visibility threshold already
        # requires games to have gone final, which cannot happen before lock, but nesting
        # it here too keeps every pick-derived surface on the page behind one single rule.
        revealed = week_is_locked(row)
        if revealed:
            columns = _build_columns(db, pool, row, games)
            # Pass the page's own resolved week explicitly, so the leaderboard always
            # matches whichever week the switcher has selected, not always the latest.
            weekly, _ = weekly_leaderboard(db, pool, week=row, viewer_id=user.id)

            # The payout column is rebuilt on the new payout engine in
            # app/services/payouts.py; wired back in here once that lands. See
            # DECISIONS.md, "Payout system".

            # Scenarios panel (Phase 8). Leverage and representative scenarios are computed
            # only for the signed in viewer (see week_scenario_panel's representative_for),
            # both being the expensive, opt-in half of the sweep; every player still gets
            # their own placement percentages.
            scenario_panel = scenario_service.week_scenario_panel(
                db, pool, row, probability_model=model, representative_for=[user.id]
            )
            if scenario_panel.report is not None:
                games_by_id = {g.id: g for g in games}
                remaining_games_by_id = {g.id: g for g in scenario_panel.remaining_games}
                viewer_outlook = scenario_panel.report.players.get(user.id)
                if viewer_outlook is not None:
                    leverage_lines = _leverage_lines(viewer_outlook, games_by_id)
                    representative_lines = _representative_lines(
                        viewer_outlook.representative_scenarios, games_by_id
                    )

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
            "weekly": weekly,
            "payouts_by_user": payouts_by_user,
            "payout_rules_exist": payout_rules_exist,
            "payout_scope": payout_scope,
            "scenario_panel": scenario_panel,
            "probability_model": model,
            "leverage_lines": leverage_lines,
            "representative_lines": representative_lines,
            "remaining_games_by_id": remaining_games_by_id,
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="results",
        pending_co_commissioner_invite=has_pending_co_commissioner_invite(db, user, pool),
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
        by_confidence: dict[int, tuple[Game, PlayerPick]] = {}
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
            cell = PlayerPick(pick.picked_team, pick.confidence, earned, state)
            cells[game.id] = cell
            by_confidence[pick.confidence] = (game, cell)

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
                by_confidence=by_confidence,
            )
        )

    sign = 1 if pool.scoring_mode == "inverse" else -1
    columns.sort(key=lambda c: (sign * c.points, -c.correct, c.display_name.lower()))
    return columns


# Build your own scenario (Phase 8) -----------------------------------------------


@router.post("/results/custom-scenario")
async def custom_scenario(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    """The three state control per remaining game (home wins / away wins / undecided),
    posted as game_<id> = "home" | "away" | "undecided", recomputed live standings for
    every player, not just the poster (spec: a social, screenshot-friendly feature). Field
    names are dynamic (one per remaining game), so this reads the raw form rather than
    typed FastAPI Form(...) parameters, the same reason app/routers/picks.py's own
    _parse_submission does.
    """
    form = await request.form()
    raw = {key: str(value) for key, value in form.items()}
    try:
        week_number = int(raw.get("week", ""))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing week.") from None

    row = _week_or_404(db, pool, week_number)
    if not week_is_locked(row):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This week has not locked yet, so there is nothing to build."
        )

    games = list(
        db.scalars(
            select(Game)
            .where(Game.week_id == row.id, Game.in_slate.is_(True))
            .order_by(Game.slate_rank)
        )
    )
    games_by_id = {g.id: g for g in games}
    remaining_ids = [g.id for g in games if g.status not in ("final", "void")]

    fixed: dict[int, str | None] = {}
    for game_id in remaining_ids:
        side = raw.get(f"game_{game_id}", "undecided")
        fixed[game_id] = side if side in ("home", "away") else None

    rows = scenario_service.custom_scenario_standings(db, pool, row, fixed)
    fixed_lines = [
        f"{games_by_id[gid].home_abbr if side == 'home' else games_by_id[gid].away_abbr} wins"
        for gid, side in fixed.items()
        if side is not None and gid in games_by_id
    ]

    return render(
        request,
        "components/custom_scenario_results.html",
        {
            "rows": rows,
            "week": row,
            "fixed_lines": fixed_lines,
            "fixed_count": len(fixed_lines),
        },
        current_user=user,
        pool=pool,
    )


__all__ = ["router"]
