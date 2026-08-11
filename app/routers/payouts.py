"""Commissioner "Set Payouts" screen (Payout system rebuild, Phase 4).

Everything here sits behind require_commissioner, same convention as app/routers/admin.py
(see that module's own docstring for the handful of exceptions elsewhere in the app; nothing
in this router is one of them). This router owns only the editor: the pot panel (entry fee,
override, weekly payout weeks, rounding, tiebreak), the four scope tables (weekly, bowl,
season_points, season_wins), scale-to-pot, and load-preset.

GET /admin/payouts/summary, GET /admin/payouts/summary.csv, POST /admin/payouts/award/{id}/paid
and POST /admin/payouts/recalculate (the player facing payout summary, marking an award paid,
and the explicit recalculate action) are a LATER phase's job, not this one; nothing here builds
them, and the editor deliberately does not link to a page that does not exist yet.

Every mutating route here (/pot, /rule, /rule/{id}/delete, /scale-to-pot, /load-preset) is a
plain form POST that redirects back to GET /admin/payouts with a 303 on both success and
failure (flash-and-redirect, exactly admin.py's own convention). This is a deliberate
simplification versus the original brief's "no page reload" ask for the live summary table: an
HTMX partial-swap version was considered, but redirecting an HTMX request on a validation
failure needs the HX-Redirect response header trick to avoid the browser's XHR silently
following a 303 and swapping a full HTML page into a small fragment container, which is real
added risk for a screen that already has a lot of validation surface area. The summary table
still updates, correctly, on every save, just via the same full page reload every other admin
form in this app already uses.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Pool, flash, require_commissioner, require_user
from app.db import get_db
from app.models import (
    PAYOUT_MODES,
    PAYOUT_ROUNDINGS,
    PAYOUT_SCOPES,
    PAYOUT_TIEBREAKS,
    PayoutRule,
    PoolMember,
    User,
)
from app.payouts import Rule, allocation_summary, resolve_rule
from app.services import payouts as payout_service
from app.templating import render

router = APIRouter(prefix="/admin/payouts", tags=["payouts"])


def _redirect() -> RedirectResponse:
    return RedirectResponse("/admin/payouts", status_code=303)


def _base(user: User, pool: Pool) -> dict:
    return {
        "current_user": user,
        "pool": pool,
        "is_commissioner": True,
        "active_nav": "admin",
    }


def _rules_by_scope(db: Session, pool: Pool) -> dict[str, list[PayoutRule]]:
    """Every PayoutRule row for this pool, grouped by scope and ordered by place. Real ORM
    rows, with a real .id, unlike app.services.payouts.load_rules's plain Rule dataclasses:
    the editor needs the id for each row's own edit/delete forms, which that function's
    dataclasses deliberately do not carry (see its own docstring)."""
    rows = list(
        db.scalars(
            select(PayoutRule)
            .where(PayoutRule.pool_id == pool.id)
            .order_by(PayoutRule.scope, PayoutRule.place)
        )
    )
    by_scope: dict[str, list[PayoutRule]] = {scope: [] for scope in PAYOUT_SCOPES}
    for row in rows:
        if row.scope in by_scope:
            by_scope[row.scope].append(row)
    return by_scope


def _editor_context(db: Session, pool: Pool) -> dict:
    paid_count = (
        db.scalar(
            select(func.count(PoolMember.id)).where(
                PoolMember.pool_id == pool.id, PoolMember.paid_at.is_not(None)
            )
        )
        or 0
    )
    computed_pot = (
        Decimal(pool.entry_fee) * paid_count if pool.entry_fee is not None else Decimal("0")
    )
    pot = payout_service.effective_pot(db, pool)

    rules_by_scope = _rules_by_scope(db, pool)

    # One display row per configured PayoutRule: the resolved dollar figure ("Resolves to")
    # plus, per the brief, the OTHER representation of the same row (the equivalent percent
    # of pot beside a dollar-mode Value input, the resolved dollar figure beside a
    # percent-mode one), both computed server side against the real effective pot so nothing
    # here duplicates app.payouts.resolve_rule's own math in a template or in JavaScript.
    rows_by_scope: dict[str, list[dict]] = {}
    all_rules: list[Rule] = []
    for scope, rows in rules_by_scope.items():
        views: list[dict] = []
        for row in rows:
            rule = Rule(
                scope=row.scope,
                place=row.place,
                mode=row.mode,
                value=Decimal(row.value),
                label=row.label,
            )
            all_rules.append(rule)
            resolved = resolve_rule(rule, pot)
            if row.mode == "amount":
                other_value = (resolved / pot * 100) if pot else Decimal("0")
            else:
                other_value = resolved
            views.append({"rule": row, "resolved": resolved, "other_value": other_value})
        rows_by_scope[scope] = views

    next_place = {
        scope: max((row.place for row in rows), default=0) + 1
        for scope, rows in rules_by_scope.items()
    }

    summary = allocation_summary(all_rules, pot=pot, weekly_weeks=pool.weekly_payout_weeks)

    return {
        "paid_count": paid_count,
        "computed_pot": computed_pot,
        "effective_pot_value": pot,
        "rows_by_scope": rows_by_scope,
        "next_place": next_place,
        "has_any_rules": any(rules_by_scope.values()),
        "summary": summary,
        "PAYOUT_SCOPES": PAYOUT_SCOPES,
        "PAYOUT_MODES": PAYOUT_MODES,
        "PAYOUT_ROUNDINGS": PAYOUT_ROUNDINGS,
        "PAYOUT_TIEBREAKS": PAYOUT_TIEBREAKS,
    }


@router.get("")
def editor(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    return render(request, "admin/payouts.html", _editor_context(db, pool), **_base(user, pool))


# The pot panel ----------------------------------------------------------------------------


def _parse_optional_dollars(raw: str, *, field_name: str, errors: list[str]) -> Decimal | None:
    """Mirrors admin.py's own entry_fee parsing exactly: blank clears the field back to unset
    (never a hard coded 0), a value that fails Decimal(str) is a clear "must be a number"
    error, and a negative value is rejected. Shared here so /pot's entry_fee and pot_override
    fields, which follow the identical rule, do not duplicate the same four lines twice."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        errors.append(f"{field_name} must be a number.")
        return None
    if value < 0:
        errors.append(f"{field_name} cannot be negative.")
    return value


@router.post("/pot")
def save_pot(
    request: Request,
    entry_fee: str = Form(""),
    pot_override: str = Form(""),
    weekly_payout_weeks: int = Form(...),
    payout_rounding: str = Form(...),
    payout_tiebreak: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    errors: list[str] = []
    fee_value = _parse_optional_dollars(entry_fee, field_name="Entry fee", errors=errors)
    override_value = _parse_optional_dollars(pot_override, field_name="Pot override", errors=errors)

    if not 0 <= weekly_payout_weeks <= 30:
        errors.append("Weekly payout weeks must be between 0 and 30.")
    if payout_rounding not in PAYOUT_ROUNDINGS:
        errors.append("Unknown rounding option.")
    if payout_tiebreak not in PAYOUT_TIEBREAKS:
        errors.append("Unknown tiebreak option.")

    if errors:
        for message in errors:
            flash(request, message, "error")
        return _redirect()

    pool.entry_fee = fee_value
    pool.pot_override = override_value
    pool.weekly_payout_weeks = weekly_payout_weeks
    pool.payout_rounding = payout_rounding
    pool.payout_tiebreak = payout_tiebreak
    db.commit()
    flash(request, "Pot settings saved.")
    return _redirect()


# One payout rule ----------------------------------------------------------------------------


@router.post("/rule")
def save_rule(
    request: Request,
    scope: str = Form(...),
    place: int = Form(...),
    mode: str = Form(...),
    value: str = Form(...),
    label: str = Form(""),
    rule_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """Create a new PayoutRule when rule_id is absent (the "Add place" form, one per scope
    section) or update the row rule_id names when present (the hidden field on each already
    configured row's own save form). This route decides create-versus-update purely from
    whether rule_id was posted, never by re-checking (scope, place) itself; see
    app.services.payouts.save_rule's own docstring for why a genuine create still cannot
    collide with an existing (scope, place) pair while an update to the same row's own place
    is not treated as a collision with itself.

    place is typed as a plain int Form field, so FastAPI itself rejects a non-integer place
    with its own 422 before this function body ever runs; everything else (unknown scope,
    unknown mode, a value that fails Decimal(str), a negative value, a percent value over 100)
    is validated here, explicitly, flash-and-redirect, never trusting the client past what
    FastAPI's own type coercion already guarantees.
    """
    errors: list[str] = []
    if scope not in PAYOUT_SCOPES:
        errors.append("Unknown payout scope.")
    if place < 1:
        errors.append("Place must be a positive number.")
    if mode not in PAYOUT_MODES:
        errors.append("Mode must be dollars or percent.")

    value_dec: Decimal | None
    try:
        value_dec = Decimal(value.strip())
    except InvalidOperation:
        errors.append("Value must be a number.")
        value_dec = None
    else:
        if value_dec < 0:
            errors.append("Value cannot be negative.")
        if mode == "percent" and value_dec > 100:
            errors.append("A percent value cannot exceed 100.")

    if errors:
        for message in errors:
            flash(request, message, "error")
        return _redirect()

    assert value_dec is not None  # every error path above already returned
    try:
        payout_service.save_rule(
            db,
            pool,
            rule_id=rule_id,
            scope=scope,
            place=place,
            mode=mode,
            value=value_dec,
            label=label.strip() or None,
        )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect()

    db.commit()
    flash(request, "Payout rule updated." if rule_id is not None else "Payout rule added.")
    return _redirect()


@router.post("/rule/{rule_id}/delete")
def remove_rule(
    request: Request,
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    if payout_service.delete_rule(db, pool, rule_id):
        db.commit()
        flash(request, "Payout rule removed.")
    else:
        db.rollback()
        flash(request, "That payout rule was already gone.", "info")
    return _redirect()


# Bulk actions ----------------------------------------------------------------------------


@router.post("/scale-to-pot")
def scale_to_pot(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    try:
        count = payout_service.scale_rules_to_pot(db, pool)
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return _redirect()

    db.commit()
    noun = "rule" if count == 1 else "rules"
    flash(request, f"Converted {count} payout {noun} to percent of the current pot.")
    return _redirect()


@router.post("/load-preset")
def load_preset(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    count = payout_service.load_preset(db, pool)
    db.commit()
    flash(request, f"Loaded the standard payout ladder ({count} rules across four scopes).")
    return _redirect()


__all__ = ["router"]
