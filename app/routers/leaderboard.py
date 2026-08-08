"""Season standings. The weekly leaderboard lives on the Results page, see
app/routers/results.py, so a single week is never shown redundantly on both pages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import get_active_pool, is_commissioner, require_user
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
    return render(
        request,
        "leaderboard.html",
        {"season": season},
        current_user=user,
        pool=pool,
        is_commissioner=is_commissioner(db, user, pool),
        active_nav="standings",
    )


__all__ = ["router"]
