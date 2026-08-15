"""Site admin: a read only listing of contact form submissions (Post-launch fixes, replacing
the old bare mailto contact page with a real form).

Gated require_admin, not require_commissioner, and kept out of app/routers/admin.py for the
exact reason app/routers/leagues.py already is (see that file's own module docstring): these
are leads for the product owner, not anything scoped to one league, and a regular
commissioner, even of their own league, should never see another visitor's contact details.

Intentionally minimal: no reply-from-here feature, no status, no assignment. The product owner
reads a submission here, then replies to that person directly from their own email client
within 24 hours. Nothing here sends an email or a text message on anyone's behalf.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models import ContactSubmission, User
from app.templating import render

router = APIRouter(prefix="/site/contacts", tags=["admin-contacts"])


@router.get("")
def contacts_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    submissions = list(
        db.scalars(select(ContactSubmission).order_by(ContactSubmission.submitted_at.desc()))
    )
    return render(
        request,
        "admin/contacts.html",
        {"submissions": submissions},
        current_user=user,
        pool=None,
        is_commissioner=True,
        active_nav="site_contacts",
    )


__all__ = ["router"]
