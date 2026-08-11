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

    # Season award panels (season points and season wins) are rebuilt on the new payout
    # engine in app/services/payouts.py; wired back in here once that lands. Placeholder
    # empty/False keeps leaderboard.html's existing conditional rendering safe in the
    # meantime. See DECISIONS.md, "Payout system".
    season_awards: dict[int, float] = {}
    show_season_awards = False

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
