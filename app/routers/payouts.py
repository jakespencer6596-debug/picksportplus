"""Commissioner "Set Payouts" screen (Payout system rebuild, Phase 4).

Everything here sits behind require_commissioner, same convention as app/routers/admin.py
(see that module's own docstring for the handful of exceptions elsewhere in the app; nothing
in this router is one of them). This router owns only the editor: the pot panel (entry fee,
override, weekly payout weeks, rounding, tiebreak), the four scope tables (weekly, bowl,
season_points, season_wins), scale-to-pot, and load-preset.

Also here (Payout system rebuild, Phase 6): GET /admin/payouts/summary, the commissioner
facing payout summary page (one row per pool member, a Paid checkbox, a running settlement
line, an unpaid-only filter); POST /admin/payouts/player/{user_id}/paid, the one-checkbox-
per-player Paid toggle that drives it, returned as an HTMX partial swap; POST
/admin/payouts/award/{id}/paid, the narrower single-award toggle kept for a later, more
granular UI; and GET /admin/payouts/summary.csv, a plain decimal CSV export. See each route's
own docstring, and app.services.payouts.mark_paid/unmark_paid, for the money-safety and
design-choice reasoning behind them. POST /admin/payouts/recalculate is still not built here;
nothing in this phase needed it.

Every mutating route here (/pot, /rule, /rule/{id}/delete, /scale-to-pot, /load-preset) is a
plain form POST that redirects back to GET /admin/payouts with a 303 on both success and
failure (flash-and-redirect, exactly admin.py's own convention). This is a deliberate
simplification versus the original brief's "no page reload" ask for the live summary table: an
HTMX partial-swap version was considered, but redirecting an HTMX request on a validation
failure needs the HX-Redirect response header trick to avoid the browser's XHR silently
following a 303 and swapping a full HTML page into a small fragment container, which is real
added risk for a screen that already has a lot of validation surface area. The summary table
still updates, correctly, on every save, just via the same full page reload every other admin
form in this app already uses. That reasoning does not carry over to the Phase 6 summary
page's own Paid checkbox: a checkbox toggle has no meaningful validation surface, so it is
built as a real HTMX partial swap (see toggle_player_paid below), not a redirect.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import Pool, flash, require_commissioner, require_user
from app.db import get_db
from app.models import (
    PAYOUT_MODES,
    PAYOUT_ROUNDINGS,
    PAYOUT_SCOPES,
    PAYOUT_TIEBREAKS,
    PayoutAward,
    PayoutRule,
    PoolMember,
    User,
)
from app.payouts import Rule, allocation_summary, resolve_rule
from app.services import payouts as payout_service
from app.services.payouts import PlayerPayoutRow
from app.templating import fmt_money, render

router = APIRouter(prefix="/admin/payouts", tags=["payouts"])


def _redirect(target: str = "/admin/payouts") -> RedirectResponse:
    return RedirectResponse(target, status_code=303)


def _redirect_to_summary(unpaid_only: bool) -> RedirectResponse:
    target = "/admin/payouts/summary" + ("?unpaid=1" if unpaid_only else "")
    return _redirect(target)


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


# Payout summary (Payout system rebuild, Phase 6) -------------------------------------------
#
# The commissioner facing summary page: one row per pool member, the running "Paid $X of $Y"
# settlement line, an unpaid-only filter, a Paid checkbox per player (toggling every one of
# that player's awards across all four scopes at once, see toggle_player_paid's own docstring
# for why), a CSV export and a copy-as-text export. Everything below reads only frozen
# PayoutAward rows via payout_service.payout_summary, never a live projection: a payout that
# has not finished scoring yet simply is not on this page, same rule the rest of this build
# already follows for a "final" figure.

_TOTAL_FIELDS = (
    "weekly_total",
    "bowl_total",
    "season_points_total",
    "season_wins_total",
    "grand_total",
    "paid_total",
    "unpaid_total",
)


def _column_totals(rows: list[PlayerPayoutRow]) -> dict[str, Decimal]:
    """Sums every money column across rows, by construction from the exact same PlayerPayoutRow
    fields the table itself renders, so a totals row built from this can never disagree with
    the rows above it."""
    zero = Decimal("0")
    return {field: sum((getattr(row, field) for row in rows), zero) for field in _TOTAL_FIELDS}


def _settlement_line(rows: list[PlayerPayoutRow]) -> dict:
    """Paid $X of $Y, N of M players settled. Always computed over EVERY pool member row,
    never just the currently filtered/displayed subset, so the headline figure at the top of
    the page does not shift just because the unpaid-only filter is toggled. A player owed
    nothing (grand_total == 0) is excluded from the M/N "settled" counts, matching the phase
    brief: a player who owes nothing was never really "unsettled" to begin with."""
    totals = _column_totals(rows)
    owed_rows = [row for row in rows if row.grand_total > 0]
    settled_rows = [row for row in owed_rows if row.unpaid_total == 0]
    return {
        "paid_sum": totals["paid_total"],
        "grand_sum": totals["grand_total"],
        "owed_count": len(owed_rows),
        "settled_count": len(settled_rows),
    }


def _text_table(pool: Pool, rows: list[PlayerPayoutRow]) -> str:
    """A monospace, fixed width plain text rendering of the summary table, built to be pasted
    straight into a group text or chat (the "Copy as text" button), not the HTML page: every
    money cell is fmt_money's own bare-number convention, no currency symbol, no "dollars"
    suffix, since a chat thread reads faster as columns of plain digits than as sentences.
    Column widths come from the widest cell actually present (header, a row, or the total
    line), never a hard coded guess, so a long display name or a four figure payout is never
    misaligned or truncated.
    """
    headers = [
        "Player",
        "Weekly",
        "Bowl",
        "Season Pts",
        "Season Wins",
        "Grand Total",
        "Paid",
        "Unpaid",
    ]

    def row_cells(row: PlayerPayoutRow) -> list[str]:
        return [
            row.display_name,
            fmt_money(row.weekly_total),
            fmt_money(row.bowl_total),
            fmt_money(row.season_points_total),
            fmt_money(row.season_wins_total),
            fmt_money(row.grand_total),
            fmt_money(row.paid_total),
            fmt_money(row.unpaid_total),
        ]

    totals = _column_totals(rows)
    total_cells = ["TOTAL"] + [fmt_money(totals[field]) for field in _TOTAL_FIELDS]

    body = [row_cells(row) for row in rows]
    all_lines = [headers, *body, total_cells]
    widths = [max(len(line[i]) for line in all_lines) for i in range(len(headers))]

    def format_line(cells: list[str]) -> str:
        first, rest = cells[0].ljust(widths[0]), cells[1:]
        return "  ".join([first] + [cell.rjust(widths[i + 1]) for i, cell in enumerate(rest)])

    lines = [f"Payout summary: {pool.name}", "", format_line(headers)]
    lines.extend(format_line(cells) for cells in body)
    lines.append(format_line(total_cells))
    return "\n".join(lines)


def _summary_context(db: Session, pool: Pool, *, unpaid_only: bool) -> dict:
    all_rows = payout_service.payout_summary(db, pool)
    display_rows = [row for row in all_rows if row.unpaid_total > 0] if unpaid_only else all_rows
    return {
        "rows": display_rows,
        "totals": _column_totals(display_rows),
        "unpaid_only": unpaid_only,
        "text_table": _text_table(pool, display_rows),
        **_settlement_line(all_rows),
    }


@router.get("/summary")
def summary(
    request: Request,
    unpaid: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    context = _summary_context(db, pool, unpaid_only=bool(unpaid))
    return render(request, "admin/payout_summary.html", context, **_base(user, pool))


@router.post("/player/{user_id}/paid")
def toggle_player_paid(
    request: Request,
    user_id: int,
    unpaid: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """The Paid checkbox the Phase 6 summary table actually uses, one per player row.

    Design choice: a player can hold PayoutAward rows in up to four scopes at once, so there
    is no single award id that means "this player's payout" the way POST
    /admin/payouts/award/{id}/paid (below) addresses one award. Rather than a checkbox per
    award nested under each player, this route marks every one of that player's currently
    unpaid awards paid in one action, or unmarks every one of their currently paid awards, in
    a single DB transaction with one db.commit() at the end, treating "Paid" as one property
    of the whole table row rather than four independent ones. Which direction it goes is read
    straight from that row's own unpaid_total: 0 means "toggle the whole row back to unpaid",
    anything else means "mark it all paid", matching the checked/unchecked state the template
    itself renders the checkbox with. A user_id that is not (or no longer) a pool member is a
    quiet no-op rather than a 404, since this only ever arrives from this page's own checkbox.

    Returns the rendered summary table partial (an HTMX partial swap over #payout-summary-wrap,
    not a redirect), so the settlement line and the totals row both stay in sync with the
    checkbox without a full page reload; unpaid, when the unpaid-only filter was active on the
    page that posted this, is carried along as a hidden hx-vals field so the swapped-in partial
    keeps the same filter the commissioner was already looking at.
    """
    rows = payout_service.payout_summary(db, pool)
    row = next((r for r in rows if r.user_id == user_id), None)
    if row is not None:
        mark_all_unpaid = row.unpaid_total == 0
        for awards in row.awards_by_scope.values():
            for award in awards:
                if mark_all_unpaid:
                    payout_service.unmark_paid(db, award.id)
                else:
                    payout_service.mark_paid(db, award.id, user)
        db.commit()

    context = _summary_context(db, pool, unpaid_only=bool(unpaid))
    return render(request, "components/payout_summary_table.html", context, **_base(user, pool))


@router.post("/award/{award_id}/paid")
def toggle_award_paid(
    request: Request,
    award_id: int,
    unpaid: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """A single award's own Paid toggle, narrower than /player/{user_id}/paid above (which is
    what the Phase 6 table itself uses). Kept for a later, more granular UI (a checkbox per
    award, nested under each player) that this phase does not build. A plain flash-and-redirect
    back to the summary page, matching the rest of this router's non-HTMX mutating routes,
    since nothing on the current summary page actually links to this route yet. An award_id
    that does not belong to this pool is a quiet no-op, never a 404, for the same double-click
    or stale-tab reason delete_rule already documents.
    """
    award = db.get(PayoutAward, award_id)
    if award is not None and award.pool_id != pool.id:
        award = None  # belongs to another pool, treated identically to "does not exist"
    if award is None:
        db.rollback()
        flash(request, "That payout award no longer exists.", "info")
        return _redirect_to_summary(bool(unpaid))

    if award.paid_at is None:
        payout_service.mark_paid(db, award_id, user)
        flash(request, "Marked paid.")
    else:
        payout_service.unmark_paid(db, award_id)
        flash(request, "Marked unpaid.")
    db.commit()
    return _redirect_to_summary(bool(unpaid))


@router.get("/summary.csv")
def summary_csv(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(require_commissioner),
):
    """Every pool member row, unfiltered (the unpaid-only view is a display convenience for the
    HTML page, not a scope this export narrows, so a commissioner reconciling the whole pot
    against a spreadsheet always gets everyone). Every numeric cell is a plain Decimal, via
    csv.writer over an io.StringIO rather than hand built strings, so a display name containing
    a comma or a quote is escaped correctly rather than corrupting the column count.
    """
    rows = payout_service.payout_summary(db, pool)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Player",
            "Weekly",
            "Bowl",
            "Season Points",
            "Season Wins",
            "Grand Total",
            "Paid",
            "Unpaid",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.display_name,
                row.weekly_total,
                row.bowl_total,
                row.season_points_total,
                row.season_wins_total,
                row.grand_total,
                row.paid_total,
                row.unpaid_total,
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="payout-summary.csv"'},
    )


__all__ = ["router"]
