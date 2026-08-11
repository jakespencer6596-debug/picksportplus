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
    # season is already every pool member (season_standings never drops anyone), so it is a
    # complete user_id -> display_name lookup for both panels, including the season_wins
    # panel, whose own display order below is not season's points-ranked order.
    member_names = {row.user_id: row.display_name for row in season}
    # The season_wins panel has no pre-ranked-by-wins StandingRow list available on this page
    # (season is ranked by points). Rather than build a fresh wins ranking from scratch, this
    # reuses the frozen PayoutAward's own place field, already correctly computed by Phase 2's
    # allocation engine, purely to decide the panel's on screen ORDER. This never touches or
    # recomputes any amount, only which row is listed first.
    season_wins_awards_ordered = sorted(
        frozen_season_awards["season_wins"], key=lambda award: award.place
    )

    return render(
        request,
        "leaderboard.html",
        {
            "season": season,
            "season_points_awards": season_points_awards,
            "season_wins_awards": season_wins_awards,
            "season_wins_awards_ordered": season_wins_awards_ordered,
            "member_names": member_names,
            "show_season_awards": show_season_awards,
        },
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="standings",
        pending_co_commissioner_invite=has_pending_co_commissioner_invite(db, user, pool),
    )


__all__ = ["router"]
