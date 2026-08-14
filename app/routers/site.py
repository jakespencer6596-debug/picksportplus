"""Site admin dashboard: a light, platform-wide landing page for the site admin.

Mounted at /site (Phase 4 remediation, see DECISIONS.md), sitting alongside
app/routers/leagues.py's own /site/leagues (league list, league creation, view-as) and
app/routers/admin_contacts.py's /site/contacts (contact form submissions). Kept in its own
file rather than folded into leagues.py so that router stays scoped to exactly what its own
module docstring says it is: league management. This one route only ever links onward to
those two, plus the ephemeral-storage health status already computed in app/config.py
(Phase 1 remediation). It does not build a provider-spend section: that is Phase 5's job
(/site/providers, not yet built) and out of scope here.

Everything here sits behind require_admin, same as every other /site/... route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models import ContactSubmission, Pool, PoolMember, User, Week
from app.templating import render

router = APIRouter(prefix="/site", tags=["site"])


def _pool_summaries(db: Session) -> list[dict]:
    """Every real pool (is_preview excluded, same query app/routers/leagues.py's own
    leagues_page already uses) with a member count and a plain-language read on where its
    season stands: the label and status of its most recently built real week, or "No weeks
    built yet" when nothing has been built. Test weeks are skipped for this, same as every
    other season-wide view in this app (Phase 3, preseason and test week support): a test
    week is not the season's current week."""
    pools = list(db.scalars(select(Pool).where(Pool.is_preview.is_(False)).order_by(Pool.name)))
    member_counts = dict(
        db.execute(
            select(PoolMember.pool_id, func.count(PoolMember.id)).group_by(PoolMember.pool_id)
        ).all()
    )
    summaries: list[dict] = []
    for pool in pools:
        latest_week = db.scalar(
            select(Week)
            .where(
                Week.pool_id == pool.id,
                Week.season_year == pool.season_year,
                Week.is_test_week.is_(False),
            )
            .order_by(Week.week_number.desc())
        )
        summaries.append(
            {
                "pool": pool,
                "member_count": member_counts.get(pool.id, 0),
                "latest_week": latest_week,
            }
        )
    return summaries


@router.get("")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    contact_count = db.scalar(select(func.count(ContactSubmission.id))) or 0
    return render(
        request,
        "admin/site_dashboard.html",
        {
            "pool_summaries": _pool_summaries(db),
            "contact_count": contact_count,
        },
        current_user=user,
        pool=None,
        is_commissioner=True,
        active_nav="site",
    )


__all__ = ["router"]
