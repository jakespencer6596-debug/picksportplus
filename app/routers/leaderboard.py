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
    from app.services.standings import season_points_ranking, season_wins_ranking

    season = season_points_ranking(db, pool, viewer_id=user.id)
    # The Season: Wins ladder (SPEC.md Section 10b), shown as its own ranked table so the
    # tiebreak note on Section 2's spec (Phase 2, "Tab entry and season tiebreak") has
    # somewhere to render independent of whether any payout scope has ever been snapshotted.
    season_by_wins = season_wins_ranking(db, pool, viewer_id=user.id)

    # Season award panels (season points and season wins), rebuilt on the new payout engine
    # in app/services/payouts.py (Payout system rebuild, Phase 5). Both panels read only
    # frozen PayoutAward rows (week_id is None for the two season scopes): season awards are
    # never projected live on this page, only ever shown once the season scope has actually
    # been snapshotted, the same "final, frozen" rule the weekly results page applies to a
    # scored week.
    frozen_season_awards = payout_service.season_awards(db, pool)
    season_points_awards = {award.user_id: award for award in frozen_season_awards["season_points"]}
    season_wins_awards = {award.user_id: award for award in frozen_season_awards["season_wins"]}
    show_season_awards = bool(season_points_awards) or bool(season_wins_awards)

    return render(
        request,
        "leaderboard.html",
        {
            "season": season,
            "season_by_wins": season_by_wins,
            "season_points_awards": season_points_awards,
            "season_wins_awards": season_wins_awards,
            "show_season_awards": show_season_awards,
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="standings",
        pending_co_commissioner_invite=has_pending_co_commissioner_invite(db, user, pool),
    )


__all__ = ["router"]
