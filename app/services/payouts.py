"""Database-backed payout service (Payout system rebuild, Phase 3).

The only layer in the payout system that touches the database. app/payouts.py (Phase 2) is
the pure, DB-free allocation engine: Rule in, Award out, no queries, no ORM objects. This
module's whole job is to translate real Pool/PayoutRule/WeekEntry/standings rows into that
engine's plain dataclasses, call it, and, for snapshot_awards/recalculate_awards, write the
result back as frozen PayoutAward rows.

The freezing matters because a percent-mode payout resolves against the pot, and the pot can
grow after a week has already been scored (a member pays their entry fee late). Re-resolving
a past week live at read time would silently change a dollar figure the commissioner may have
already paid out over Venmo. PayoutAward is the fix: the instant a week (or the season)
finishes scoring, the resolved amount, the pot it was computed against, and the rule in force
at that moment are written once and never drift again on their own. snapshot_awards is the
idempotent, automatic path (called from the scoring hook); recalculate_awards is the only
place a frozen figure is ever deliberately overwritten, and it is never called implicitly.

Money is Decimal end to end in this module, never float, no exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PayoutAward, PayoutRule, Pool, PoolMember, User, Week, WeekEntry, utcnow
from app.payouts import SCOPES, Award, Rule, Standing, StandingInput, allocate, rank_standings
from app.services.standings import season_standings, weekly_leaderboard

__all__ = [
    "PlayerPayoutRow",
    "effective_pot",
    "load_rules",
    "project_awards",
    "snapshot_awards",
    "awards_for_week",
    "season_awards",
    "payout_summary",
    "mark_paid",
    "recalculate_awards",
]


@dataclass
class PlayerPayoutRow:
    """One row per pool member for the /admin/payouts summary page.

    awards_by_scope groups the raw, frozen PayoutAward rows by scope ("weekly", "bowl",
    "season_points", "season_wins") rather than handing back a flat list of award ids. This
    matches how the summary page is expected to render, one section per scope, and lets a
    later phase iterate a member's awards_by_scope["weekly"] directly to draw a per-award
    "mark paid" checkbox without a second query or re-deriving the grouping itself.

    paid_total only counts awards that already have paid_at set; unpaid_total is
    grand_total - paid_total, not a second independent sum, so the two are always
    reconcilable by construction (no code path can make them disagree).
    """

    user_id: int
    display_name: str
    weekly_total: Decimal
    bowl_total: Decimal
    season_points_total: Decimal
    season_wins_total: Decimal
    grand_total: Decimal
    paid_total: Decimal
    unpaid_total: Decimal
    awards_by_scope: dict[str, list[PayoutAward]] = field(default_factory=dict)


def effective_pot(db: Session, pool: Pool) -> Decimal:
    """The real, current pot: pool.pot_override when the commissioner has set one (always
    wins, even over a nonzero computed figure), otherwise entry_fee times the number of
    members who have actually paid. Decimal("0"), never None, when entry_fee is unset or
    nobody has paid yet.
    """
    if pool.pot_override is not None:
        return Decimal(pool.pot_override)
    if pool.entry_fee is None:
        return Decimal("0")
    paid_count = (
        db.scalar(
            select(func.count(PoolMember.id)).where(
                PoolMember.pool_id == pool.id, PoolMember.paid_at.is_not(None)
            )
        )
        or 0
    )
    return Decimal(pool.entry_fee) * paid_count


def load_rules(db: Session, pool: Pool, scope: str | None = None) -> list[Rule]:
    """Every PayoutRule row for the pool, optionally filtered to one scope, as plain
    app.payouts.Rule dataclasses (never ORM objects), ordered by place."""
    stmt = select(PayoutRule).where(PayoutRule.pool_id == pool.id)
    if scope is not None:
        stmt = stmt.where(PayoutRule.scope == scope)
    stmt = stmt.order_by(PayoutRule.place)
    return [
        Rule(
            scope=row.scope,
            place=row.place,
            mode=row.mode,
            value=Decimal(row.value),
            label=row.label,
        )
        for row in db.scalars(stmt)
    ]


def _direction_descending(pool: Pool) -> bool:
    """weekly/bowl/season_points all read the pool's own scoring direction: "standard" ranks
    highest first (descending), "inverse" ranks lowest first (ascending). season_wins never
    calls this, its direction is hard coded True at its own call site below, on purpose."""
    return pool.scoring_mode == "standard"


def _weekly_or_bowl_standings(
    db: Session, pool: Pool, scope: str, week: Week | None
) -> list[Standing]:
    if week is None:
        raise ValueError(f"scope {scope!r} requires an explicit week")
    rows, _ = weekly_leaderboard(db, pool, week=week)
    # Matches the shape of the old, deleted service's weekly_payouts helper: submitted_at
    # comes straight from WeekEntry for this week, keyed by user_id, and a player with no
    # entry row (or no submission) simply has no key here, so StandingInput.submitted_at
    # falls back to its own None default, which app.payouts.allocate's tiebreak sorts last.
    submitted_by_user = {
        entry.user_id: entry.submitted_at
        for entry in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id))
    }
    inputs = [
        StandingInput(
            user_id=row.user_id,
            metric=Decimal(row.points),
            submitted_at=submitted_by_user.get(row.user_id),
        )
        for row in rows
        # A no-show does not win money, even though under scoring_mode "inverse" their raw
        # points can be a real, nonzero maximum-penalty value that might otherwise place.
        if not row.did_not_submit
    ]
    return rank_standings(inputs, descending=_direction_descending(pool))


def _season_points_standings(db: Session, pool: Pool) -> list[Standing]:
    rows = season_standings(db, pool)
    # No single instant in this codebase represents "the season started" (there is no stored
    # season-start timestamp anywhere), so every player gets submitted_at=None here, on
    # purpose. That falls back to app.payouts.allocate's own documented None-sorts-last,
    # then-user_id remainder tiebreak, which is deterministic, not a bug: there is simply no
    # more meaningful signal available for a season-wide scope.
    inputs = [
        StandingInput(user_id=row.user_id, metric=Decimal(row.points), submitted_at=None)
        for row in rows
    ]
    return rank_standings(inputs, descending=_direction_descending(pool))


def _season_wins_standings(db: Session, pool: Pool) -> list[Standing]:
    rows = season_standings(db, pool)
    # season_wins ranks by weekly win count, ALWAYS descending, regardless of pool.scoring_mode.
    # StandingRow.rank cannot be reused here: it is always assigned in the pool's points
    # direction (see app/services/standings.py._points_sort_key), which means something
    # different from "most weekly wins first" whenever scoring_mode is "inverse". Building a
    # fresh StandingInput list from weekly_wins and calling rank_standings ourselves, with
    # descending=True hard coded at this call site (never read from pool.scoring_mode), is
    # exactly what app/payouts.py's own module docstring calls out as the single easiest
    # wiring mistake a caller could make here.
    inputs = [
        StandingInput(user_id=row.user_id, metric=Decimal(row.weekly_wins), submitted_at=None)
        for row in rows
    ]
    return rank_standings(inputs, descending=True)


def _standings_for_scope(db: Session, pool: Pool, scope: str, week: Week | None) -> list[Standing]:
    if scope in ("weekly", "bowl"):
        return _weekly_or_bowl_standings(db, pool, scope, week)
    if scope == "season_points":
        return _season_points_standings(db, pool)
    if scope == "season_wins":
        return _season_wins_standings(db, pool)
    raise ValueError(f"Unknown payout scope: {scope!r}")


def project_awards(db: Session, pool: Pool, scope: str, week: Week | None = None) -> list[Award]:
    """Live, unsaved. For "weekly"/"bowl" the caller must pass the relevant week explicitly
    (for a bowl-scope call, the caller passes the bowl week itself; this function does not
    resolve which week "is" the bowl week, mirroring weekly_leaderboard's own explicit-week
    contract). Nothing here is written to the database."""
    rules = load_rules(db, pool, scope=scope)
    standings = _standings_for_scope(db, pool, scope, week)
    pot = effective_pot(db, pool)
    return allocate(
        rules, standings, pot=pot, rounding=pool.payout_rounding, tiebreak=pool.payout_tiebreak
    )


def _existing_award(
    db: Session, pool: Pool, scope: str, week_id: int | None, user_id: int
) -> PayoutAward | None:
    """Explicit query by (pool_id, scope, week_id, user_id), never relying on the DB's own
    unique constraint or an upsert. The unique constraint on payout_awards is exactly this
    tuple, but standard SQL treats NULL as distinct from NULL, so for the two season scopes
    (week_id always None) the database itself never catches a duplicate; this query is what
    actually makes snapshot_awards/recalculate_awards idempotent for every scope, not just
    weekly/bowl. See test_null_week_id_is_not_covered_by_the_unique_constraint in
    tests/test_payout_models.py for the underlying gap this closes."""
    return db.scalar(
        select(PayoutAward).where(
            PayoutAward.pool_id == pool.id,
            PayoutAward.scope == scope,
            PayoutAward.week_id == week_id,
            PayoutAward.user_id == user_id,
        )
    )


def snapshot_awards(
    db: Session, pool: Pool, scope: str, week: Week | None = None, actor: User | None = None
) -> list[PayoutAward]:
    """Write PayoutAward rows once, freezing amount/pot_at_award/rule_mode/rule_value at this
    moment. Idempotent: a second call with nothing changed underneath must not create
    duplicates and must not touch an existing row's money fields, that overwrite behavior
    belongs only to recalculate_awards, a separate, explicitly invoked function. actor is
    accepted for interface symmetry with recalculate_awards/mark_paid but unused here: an
    ordinary snapshot is an automatic side effect of scoring, not an attributable admin action,
    so nothing on PayoutAward records who triggered a plain snapshot.
    """
    del actor
    week_id = week.id if week else None
    pot = effective_pot(db, pool)
    live_awards = project_awards(db, pool, scope, week=week)

    for award in live_awards:
        existing = _existing_award(db, pool, scope, week_id, award.user_id)
        if existing is not None:
            # The idempotent no-op case: leave the frozen row's money fields exactly as they
            # were, even if the live projection would now compute something different.
            continue
        db.add(
            PayoutAward(
                pool_id=pool.id,
                user_id=award.user_id,
                scope=scope,
                week_id=week_id,
                place=award.place,
                tied_with=award.tied_with,
                amount=award.amount,
                pot_at_award=pot,
                rule_mode=award.rule_mode,
                rule_value=award.rule_value,
                awarded_at=utcnow(),
            )
        )

    db.flush()
    return list(
        db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == pool.id,
                PayoutAward.scope == scope,
                PayoutAward.week_id == week_id,
            )
        )
    )


def awards_for_week(db: Session, pool: Pool, week: Week) -> list[PayoutAward]:
    """Every PayoutAward for this pool and this week_id, whatever scope wrote it (weekly and
    bowl rows can in principle share a week_id, so this filters by pool and week only)."""
    return list(
        db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == pool.id, PayoutAward.week_id == week.id
            )
        )
    )


def season_awards(db: Session, pool: Pool) -> dict[str, list[PayoutAward]]:
    """{"season_points": [...], "season_wins": [...]}, each list every PayoutAward row for
    that pool/scope with week_id is None."""
    result: dict[str, list[PayoutAward]] = {"season_points": [], "season_wins": []}
    rows = db.scalars(
        select(PayoutAward).where(
            PayoutAward.pool_id == pool.id,
            PayoutAward.week_id.is_(None),
            PayoutAward.scope.in_(("season_points", "season_wins")),
        )
    )
    for row in rows:
        result[row.scope].append(row)
    return result


def payout_summary(db: Session, pool: Pool) -> list[PlayerPayoutRow]:
    """One row per pool member, even a member with zero awards (all totals Decimal("0")),
    sorted by grand_total descending then display_name. Reads only frozen PayoutAward rows
    across all four scopes, never calls project_awards."""
    members = list(
        db.scalars(
            select(User)
            .join(PoolMember, PoolMember.user_id == User.id)
            .where(PoolMember.pool_id == pool.id)
            .order_by(User.display_name)
        )
    )
    awards_by_user: dict[int, list[PayoutAward]] = {}
    for award in db.scalars(select(PayoutAward).where(PayoutAward.pool_id == pool.id)):
        awards_by_user.setdefault(award.user_id, []).append(award)

    rows: list[PlayerPayoutRow] = []
    for member in members:
        totals: dict[str, Decimal] = {scope: Decimal("0") for scope in SCOPES}
        awards_by_scope: dict[str, list[PayoutAward]] = {scope: [] for scope in SCOPES}
        paid_total = Decimal("0")
        for award in awards_by_user.get(member.id, []):
            totals[award.scope] += award.amount
            awards_by_scope[award.scope].append(award)
            if award.paid_at is not None:
                paid_total += award.amount

        grand_total = sum(totals.values(), Decimal("0"))
        rows.append(
            PlayerPayoutRow(
                user_id=member.id,
                display_name=member.display_name,
                weekly_total=totals["weekly"],
                bowl_total=totals["bowl"],
                season_points_total=totals["season_points"],
                season_wins_total=totals["season_wins"],
                grand_total=grand_total,
                paid_total=paid_total,
                unpaid_total=grand_total - paid_total,
                awards_by_scope=awards_by_scope,
            )
        )

    rows.sort(key=lambda r: (-r.grand_total, r.display_name.lower()))
    return rows


def mark_paid(db: Session, award_id: int, actor: User) -> PayoutAward:
    """Set paid_at/paid_marked_by_user_id. Idempotent: calling this again on an
    already-paid award leaves the original paid_at untouched and still returns the row
    without error."""
    award = db.get(PayoutAward, award_id)
    if award is None:
        raise ValueError(f"No payout award with id {award_id!r}")
    if award.paid_at is None:
        award.paid_at = utcnow()
        award.paid_marked_by_user_id = actor.id
    db.flush()
    return award


def recalculate_awards(
    db: Session, pool: Pool, scope: str, week: Week | None, actor: User
) -> list[PayoutAward]:
    """The one explicit, deliberate overwrite path. Recomputes live and stamps every matching
    row's amount/pot_at_award/rule_mode/rule_value/place/tied_with plus recalculated_at/
    recalculated_by_user_id, while explicitly leaving paid_at/paid_marked_by_user_id exactly
    as they were: the whole point is a commissioner never loses payment tracking just because
    a recalculation ran. A player whose award vanished from the live projection (rules changed,
    they no longer place) is left in place rather than deleted: given this build's money-safety
    stance, a stale-but-present row is a safer failure mode than silently deleting a row that
    might already be marked paid. A later phase's UI is expected to flag such a stale award
    rather than this function removing it.
    """
    week_id = week.id if week else None
    pot = effective_pot(db, pool)
    live_awards = project_awards(db, pool, scope, week=week)
    now = utcnow()

    for award in live_awards:
        existing = _existing_award(db, pool, scope, week_id, award.user_id)
        if existing is None:
            db.add(
                PayoutAward(
                    pool_id=pool.id,
                    user_id=award.user_id,
                    scope=scope,
                    week_id=week_id,
                    place=award.place,
                    tied_with=award.tied_with,
                    amount=award.amount,
                    pot_at_award=pot,
                    rule_mode=award.rule_mode,
                    rule_value=award.rule_value,
                    awarded_at=now,
                    recalculated_at=now,
                    recalculated_by_user_id=actor.id,
                )
            )
            continue
        existing.amount = award.amount
        existing.pot_at_award = pot
        existing.rule_mode = award.rule_mode
        existing.rule_value = award.rule_value
        existing.place = award.place
        existing.tied_with = award.tied_with
        existing.recalculated_at = now
        existing.recalculated_by_user_id = actor.id
        # paid_at / paid_marked_by_user_id intentionally untouched.

    db.flush()
    return list(
        db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == pool.id,
                PayoutAward.scope == scope,
                PayoutAward.week_id == week_id,
            )
        )
    )
