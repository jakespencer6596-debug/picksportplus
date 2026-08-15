"""The preview pool: a hidden, non-customer Pool that exists purely to hold a real, live game
slate for a signed in visitor who has not joined a league yet.

Design (Post-launch fixes, see DECISIONS.md): reuse the exact same build_slate machinery
every real pool's slate already goes through (app/services/ingest.py), against one pool with
Pool.is_preview=True, rather than a second, parallel slate-computation code path. Exactly one
such pool should exist in a given deployment. It has no commissioner and no PoolMember rows,
ever: app/routers/leagues.py excludes is_preview pools from the admin league listing, and the
multi-league switchers in app/routers/admin.py already only ever match a real PoolMember row,
which this pool never has.

Building or refreshing its slate is never triggered from inside a request handler, so an
anonymous or poolless visitor's page load can never itself spend a metered provider credit.
It is created (find-or-create) only by the explicit, human-triggered
`python -m app.cli seed-preview` command, the same "deliberate trigger" contract every real
pool's own creation follows.

Once it exists, `python -m app.cli run-cron` (Phase 8 remediation, see DECISIONS.md) refreshes
its slate on the same hourly, operator-scheduled cadence every real pool's slate already gets,
through the same `sync_week`, with the same metered-budget carefulness (the site admin's
global "ESPN only" switch, the per-week refresh cap): a scheduled cron tick is a controlled,
operator-owned trigger, not a visitor's page view, so refreshing the preview there does not
reopen the "no request handler ever spends a credit" guarantee above. `run-cron` never calls
`ensure_preview_pool`, only the read only `get_preview_pool`, so a deployment that has never
run `seed-preview` once by hand still never gets a preview pool conjured out from under it by
an unattended cron tick; `python -m app.cli doctor` flags that missing-or-stale state so an
operator notices.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Pool

PREVIEW_POOL_NAME = "PickSportPlus Preview"


def get_preview_pool(db: Session) -> Pool | None:
    """The one is_preview pool, if it has been seeded yet.

    Read only, safe to call from inside a request handler: never creates one.
    """
    return db.scalar(select(Pool).where(Pool.is_preview.is_(True)))


def ensure_preview_pool(db: Session) -> Pool:
    """Find, or create, the one preview pool.

    Only ever called from the seed-preview CLI command: an explicit, human-triggered action,
    never from inside a request handler. Same seed defaults seed_admin (app/cli.py) uses for
    num_games_per_week/target_nfl/target_ncaaf (20/8/12), the current real season year, and
    the site's configured week1_anchor_date if one is set (detect_week's existing fallback
    handles it exactly like any other freshly seeded pool when it is not). No commissioner, no
    PoolMember rows: this pool exists purely to hold Week/Game rows through the ordinary slate
    pipeline.
    """
    from app.auth import generate_join_code

    pool = get_preview_pool(db)
    if pool is not None:
        return pool

    code = generate_join_code()
    for _ in range(10):
        if db.scalar(select(Pool).where(func.upper(Pool.join_code) == code)) is None:
            break
        code = generate_join_code()

    pool = Pool(
        name=PREVIEW_POOL_NAME,
        join_code=code,
        season_year=settings.season_year,
        num_games_per_week=settings.num_games_per_week,
        target_nfl=settings.nfl_games_per_week,
        target_ncaaf=settings.ncaaf_games_per_week,
        sports=["nfl", "ncaaf"],
        open_registration=False,
        timezone=settings.timezone,
        current_week=1,
        week1_anchor_date=settings.week1_anchor_date,
        is_preview=True,
    )
    db.add(pool)
    db.flush()
    return pool


__all__ = ["get_preview_pool", "ensure_preview_pool", "PREVIEW_POOL_NAME"]
