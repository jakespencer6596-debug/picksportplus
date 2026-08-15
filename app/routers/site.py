"""Site admin dashboard and provider controls: light, platform-wide pages for the site admin.

Mounted at /site (Phase 4 remediation, see DECISIONS.md), sitting alongside
app/routers/leagues.py's own /site/leagues (league list, league creation, view-as) and
app/routers/admin_contacts.py's /site/contacts (contact form submissions). Kept in its own
file rather than folded into leagues.py so that router stays scoped to exactly what its own
module docstring says it is: league management. The dashboard route only ever links onward to
those two, plus /site/providers (below) and the ephemeral-storage health status already
computed in app/config.py (Phase 1 remediation).

/site/providers and its POST toggle (Phase 5 remediation: provider controls move to site
admin) moved the old commissioner-facing "Provider budgets" section off /league entirely: a
commissioner cannot reason about a metered API credit, so this whole surface, key presence,
spend against the cap, the last call, and the global "ESPN only" switch, is site admin only
now. See DECISIONS.md, Phase 5, for the full reasoning.

Everything here sits behind require_admin, same as every other /site/... route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import flash, is_valid_email_format, normalize_email, require_admin
from app.config import settings
from app.db import get_db
from app.models import ContactSubmission, MailLog, Pool, PoolMember, User, Week
from app.providers.http import get_platform_settings, provider_warnings, usage_report
from app.services import mail
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


# Provider controls ------------------------------------------------------------


@router.get("/providers")
def providers_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Everything about the metered feeds, site admin only (Phase 5 remediation).

    usage_report(db) is reused verbatim, the same view model the old /league "Provider
    budgets" section rendered: which provider keys are configured (presence only, never the
    value, the same 'set'/'NOT SET' style check app/cli.py's doctor command already uses),
    calls made this month and spend against the cap, and the most recent call and its status.
    provider_warnings(db) is the same exhausted-budget/last-error surfacing the old dashboard
    and the slate page already use. espn_only is the global switch's current, freshly read
    value, toggled below by POST /site/providers/espn-only.
    """
    return render(
        request,
        "admin/site_providers.html",
        {
            "usage": usage_report(db),
            "warnings": provider_warnings(db),
            "espn_only": get_platform_settings(db).espn_only,
        },
        current_user=user,
        pool=None,
        is_commissioner=True,
        active_nav="site_providers",
    )


@router.post("/providers/espn-only")
def providers_espn_only_toggle(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Flip the global "ESPN only" switch and redirect back. A plain toggle, not a form field
    with a value, since the page only ever shows one button: turn it on, or turn it off,
    whichever is not the current state (see admin/site_providers.html)."""
    setting = get_platform_settings(db)
    setting.espn_only = not setting.espn_only
    db.commit()
    if setting.espn_only:
        flash(
            request,
            "ESPN only is on. Every league's slate build skips The Odds API and "
            "CollegeFootballData until you turn this back off.",
        )
    else:
        flash(request, "ESPN only is off. Slate builds use the full provider path again.")
    return RedirectResponse("/site/providers", status_code=303)


# Mail panel -------------------------------------------------------------------

MAIL_LOG_PAGE_SIZE = 50


@router.get("/mail")
def mail_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Site admin only (Phase 7 remediation, see DECISIONS.md): configuration status (presence
    only, never the key value, the same "set"/"NOT SET" style /site/providers already uses for
    ESPN key presence), the most recent sends from MailLog so the site admin can prove whether
    something actually went out, and a test-send form that exercises the exact same
    mail.send() path every other feature uses, no special cased test-only send."""
    recent = list(
        db.scalars(select(MailLog).order_by(MailLog.created_at.desc()).limit(MAIL_LOG_PAGE_SIZE))
    )
    return render(
        request,
        "admin/site_mail.html",
        {
            "mail_enabled": settings.mail_enabled,
            "key_configured": bool(settings.resend_api_key),
            "from_configured": bool(settings.mail_from_address),
            "rate_limit": settings.mail_rate_limit_per_hour,
            "recent": recent,
            "test_email_default": user.email,
        },
        current_user=user,
        pool=None,
        is_commissioner=True,
        active_nav="site_mail",
    )


@router.post("/mail/test")
def mail_test_send(
    request: Request,
    email: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    address = normalize_email(email) or user.email
    if not is_valid_email_format(address):
        flash(request, "Enter a valid email address.", "error")
        return RedirectResponse("/site/mail", status_code=303)

    subject = "PickSportPlus test email"
    body = (
        "Hey,\n\n"
        "This is a test email from PickSportPlus's site admin mail panel. If it reached you, "
        "sending works."
    )
    try:
        mail.send(
            db,
            to=address,
            subject=subject,
            html=mail.text_to_html(body),
            text=body,
            kind="test",
            actor_key=f"user:{user.id}",
        )
    except mail.MailError as exc:
        db.commit()
        flash(request, str(exc), "error")
    else:
        db.commit()
        flash(request, f"Test email sent to {address}.")
    return RedirectResponse("/site/mail", status_code=303)


__all__ = ["router"]
