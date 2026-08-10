"""Season standings. The weekly leaderboard lives on the Results page, see
app/routers/results.py, so a single week is never shown redundantly on both pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import (
    get_active_pool,
    has_pending_co_commissioner_invite,
    is_commissioner,
    require_user,
)
from app.db import get_db
from app.models import Pool, User
from app.services import payouts as payout_service
from app.templating import render

router = APIRouter(tags=["standings"])


@router.get("/standings")
def standings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
):
    from app.services.standings import season_standings

    season = season_standings(db, pool, viewer_id=user.id)

    # Season awards panel (Phase 7): driven by PayoutRule rows of scope "season". There is no
    # field in this codebase asserting a season is officially over, so this shows as soon as
    # at least one week has been scored, worded as the pool's current standings rather than a
    # final result. See DECISIONS.md, Phase 7, for why this gate was chosen over the
    # alternatives considered.
    season_has_scores = any(row.weeks_played > 0 for row in season)
    season_awards: dict[int, float] = {}
    show_season_awards = False
    if season_has_scores:
        season_rules = payout_service.rules_by_place(db, pool, "season")
        if season_rules:
            show_season_awards = True
            season_awards = payout_service.season_payouts(season, season_rules)

    return render(
        request,
        "leaderboard.html",
        {
            "season": season,
            "season_awards": season_awards,
            "show_season_awards": show_season_awards,
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="standings",
        pending_co_commissioner_invite=has_pending_co_commissioner_invite(db, user, pool),
    )


__all__ = ["router"]
