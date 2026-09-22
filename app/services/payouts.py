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
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PayoutAward, PayoutRule, Pool, PoolMember, User, Week, utcnow
from app.payouts import SCOPES, Award, Rule, Standing, allocate, resolve_rule
from app.services.standings import season_points_ranking, season_wins_ranking, weekly_leaderboard

__all__ = [
    "PlayerPayoutRow",
    "AwardDiff",
    "effective_pot",
    "load_rules",
    "project_awards",
    "recalculate_preview",
    "snapshot_awards",
    "awards_for_week",
    "season_awards",
    "payout_summary",
    "mark_paid",
    "unmark_paid",
    "recalculate_awards",
    "find_rule",
    "save_rule",
    "delete_rule",
    "scale_rules_to_pot",
    "load_preset",
]


@dataclass
class PlayerPayoutRow:
    """One row per pool member for the /league/payouts summary page.

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


def _weekly_or_bowl_standings(
    db: Session, pool: Pool, scope: str, week: Week | None
) -> list[Standing]:
    """weekly_leaderboard already applies the league's one tie rule (points, then prior wins
    entering the week, then split, standings and ties, September, unconditionally for every
    pool), so this only ever reuses its rank, never re-derives one: two players still tied
    after wins keep the same shared rank here so allocate() splits the combined payout for the
    places they span, exactly as weekly_leaderboard itself displays.
    """
    if week is None:
        raise ValueError(f"scope {scope!r} requires an explicit week")
    rows, _ = weekly_leaderboard(db, pool, week=week)
    # A no-show does not win money, even though under scoring_mode "inverse" their raw points
    # can be a real, nonzero maximum-penalty value that might otherwise place.
    rows = [row for row in rows if not row.did_not_submit]

    # weekly_leaderboard's own rank was assigned across the full roster including whichever
    # no-shows the filter above just removed, which would otherwise leave gaps (rank 2, 3, ...
    # instead of 1, 2, ...) that allocate() would silently skip as "past the last real place."
    # Re-numbering here, while preserving whichever rows already shared a rank (a genuine
    # points-and-wins tie), keeps the group structure allocate() needs to split correctly.
    standings: list[Standing] = []
    next_rank = 0
    last_source_rank: int | None = None
    for index, row in enumerate(rows, start=1):
        if row.rank != last_source_rank:
            next_rank = index
            last_source_rank = row.rank
        standings.append(
            Standing(
                user_id=row.user_id,
                rank=next_rank,
                metric=Decimal(row.points),
                submitted_at=None,
                display_name=row.display_name,
            )
        )
    return standings


def _season_points_standings(db: Session, pool: Pool) -> list[Standing]:
    """season_points_ranking already applies the league's one season tie rule (points, then
    weekly wins, then split, standings and ties, September, unconditionally for every pool):
    reuse its rank directly, never re-derive one, so a genuine tie keeps the shared rank
    allocate() needs to split the combined payout across.
    """
    ranked = season_points_ranking(db, pool)
    return [
        Standing(
            user_id=row.user_id,
            rank=row.rank,
            metric=Decimal(row.points),
            submitted_at=None,
            display_name=row.display_name,
        )
        for row in ranked
    ]


def _season_wins_standings(db: Session, pool: Pool) -> list[Standing]:
    """Same reasoning as _season_points_standings, mirrored for the wins ladder (weekly wins,
    then season points, then split): reuse season_wins_ranking's own rank directly.
    """
    ranked = season_wins_ranking(db, pool)
    return [
        Standing(
            user_id=row.user_id,
            rank=row.rank,
            metric=Decimal(row.weekly_wins),
            submitted_at=None,
            display_name=row.display_name,
        )
        for row in ranked
    ]


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
    # The remainder cent goes to the tied players in alphabetical order of display name
    # (standings and ties, September, the league's own rule): fixed for every pool, never a
    # per pool setting any more. pool.payout_tiebreak is left entirely unread here; see
    # DECISIONS.md, "Standings and ties, September".
    return allocate(
        rules, standings, pot=pot, rounding=pool.payout_rounding, tiebreak="alphabetical"
    )


@dataclass
class AwardDiff:
    """One player whose frozen award would change if recalculate_awards ran right now
    (standings and ties, September, Phase 2, "make the recalculate button safe under the new
    rule"). old_* is None for a player the live projection newly places who has no frozen row
    yet; new_* is None for a player who no longer places live (recalculate_awards leaves a
    stale row in place rather than deleting it, so this is shown, not silently dropped)."""

    user_id: int
    display_name: str
    old_place: int | None
    new_place: int | None
    old_amount: Decimal | None
    new_amount: Decimal | None
    paid: bool


def recalculate_preview(
    db: Session, pool: Pool, scope: str, week: Week | None = None
) -> list[AwardDiff]:
    """What recalculate_awards would change for this scope, computed but never written
    (standings and ties, September, Phase 2). Only players whose place or amount would
    actually differ are returned; a player the live projection agrees with the frozen row on
    is left out entirely, so an empty list means "safe, nothing would change."
    """
    week_id = week.id if week else None
    live_awards = {a.user_id: a for a in project_awards(db, pool, scope, week=week)}
    frozen_awards = {
        a.user_id: a
        for a in db.scalars(
            select(PayoutAward).where(
                PayoutAward.pool_id == pool.id,
                PayoutAward.scope == scope,
                PayoutAward.week_id == week_id,
            )
        )
    }
    names = {
        u.id: u.display_name
        for u in db.scalars(
            select(User)
            .join(PoolMember, PoolMember.user_id == User.id)
            .where(PoolMember.pool_id == pool.id)
        )
    }

    diffs: list[AwardDiff] = []
    for user_id in set(live_awards) | set(frozen_awards):
        live = live_awards.get(user_id)
        frozen = frozen_awards.get(user_id)
        old_place = frozen.place if frozen else None
        old_amount = frozen.amount if frozen else None
        new_place = live.place if live else None
        new_amount = live.amount if live else None
        if old_place == new_place and old_amount == new_amount:
            continue
        diffs.append(
            AwardDiff(
                user_id=user_id,
                display_name=names.get(user_id, f"User {user_id}"),
                old_place=old_place,
                new_place=new_place,
                old_amount=old_amount,
                new_amount=new_amount,
                paid=bool(frozen and frozen.paid_at is not None),
            )
        )
    diffs.sort(key=lambda d: d.display_name.lower())
    return diffs


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


def unmark_paid(db: Session, award_id: int) -> PayoutAward:
    """Clear paid_at/paid_marked_by_user_id back to None, the mirror of mark_paid. Idempotent:
    calling this on an award that is already unpaid is a harmless no-op that still returns the
    row without error. Together, mark_paid/unmark_paid are what let the summary page's Paid
    checkbox (Payout system rebuild, Phase 6) be a real two-way toggle rather than a
    one-directional stamp: a commissioner who fat-fingers a mark-paid click can undo it."""
    award = db.get(PayoutAward, award_id)
    if award is None:
        raise ValueError(f"No payout award with id {award_id!r}")
    if award.paid_at is not None:
        award.paid_at = None
        award.paid_marked_by_user_id = None
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


# Set Payouts editor (Payout system rebuild, Phase 4) ------------------------------------
#
# Everything below this line supports the commissioner-facing /league/payouts screen
# (app/routers/payouts.py): plain create/update/delete for a single PayoutRule row, plus
# the two bulk actions the editor offers (scale every rule to the pot, load the known
# preset ladder). Route-level input shape/range validation (place is a positive integer,
# scope/mode are known values, value parses as Decimal, a percent value is capped at 100,
# weekly_payout_weeks is 0-30) lives in the router, not here, matching this codebase's own
# router/service split; what lives here is the part a raw HTML form cannot check for itself,
# identity (does rule_id name a row that is really this pool's) and the uniqueness rule the
# database would otherwise enforce with a raw IntegrityError.


def find_rule(db: Session, pool: Pool, rule_id: int) -> PayoutRule | None:
    """The PayoutRule row for rule_id, but only if it actually belongs to this pool. A rule
    id from another pool (or one that no longer exists) reads as None, never someone else's
    row, so a caller never has to double check pool_id itself."""
    row = db.get(PayoutRule, rule_id)
    if row is None or row.pool_id != pool.id:
        return None
    return row


def save_rule(
    db: Session,
    pool: Pool,
    *,
    rule_id: int | None,
    scope: str,
    place: int,
    mode: str,
    value: Decimal,
    label: str | None,
) -> PayoutRule:
    """Create a new PayoutRule (rule_id is None, the "Add place" form on the editor) or
    update the row rule_id names (a hidden field on each already-configured row's own save
    form). This is the one place in the payout system that decides "create" versus "update":
    the editor never posts an id for a brand new row and always posts one for an existing
    row, so the caller's intent is unambiguous from rule_id alone.

    Raises ValueError, never letting the database's own (pool_id, scope, place) unique
    constraint raise a raw IntegrityError, in two cases: rule_id names a row that is not
    (or no longer) this pool's, and a genuine (scope, place) collision with a DIFFERENT row
    (updating a row to the scope/place it already has is not a collision with itself, that is
    the ordinary "change the value, keep the place" edit). The pre-check here, a SELECT before
    any write, is deliberate: it is what lets the route give the commissioner a clean flash
    message instead of a 500. Does not commit; the caller owns the transaction.
    """
    existing: PayoutRule | None = None
    if rule_id is not None:
        existing = find_rule(db, pool, rule_id)
        if existing is None:
            raise ValueError("That payout rule no longer exists.")

    conflict = db.scalar(
        select(PayoutRule).where(
            PayoutRule.pool_id == pool.id, PayoutRule.scope == scope, PayoutRule.place == place
        )
    )
    if conflict is not None and (existing is None or conflict.id != existing.id):
        raise ValueError(f"Place {place} is already configured for that scope.")

    if existing is None:
        row = PayoutRule(
            pool_id=pool.id, scope=scope, place=place, mode=mode, value=value, label=label
        )
        db.add(row)
    else:
        existing.scope = scope
        existing.place = place
        existing.mode = mode
        existing.value = value
        existing.label = label
        row = existing
    db.flush()
    return row


def delete_rule(db: Session, pool: Pool, rule_id: int) -> bool:
    """True if a row belonging to this pool was actually removed. False for a rule_id that
    names nothing, or someone else's pool's rule: the delete route treats both the same, a
    quiet no-op rather than a 404 dead end, which is friendlier to a commissioner who double
    clicks remove or has a stale tab open. Does not commit."""
    row = find_rule(db, pool, rule_id)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def scale_rules_to_pot(db: Session, pool: Pool) -> int:
    """Convert every PayoutRule row for this pool, across all four scopes, to mode="percent"
    at its CURRENT effective share of the pot, so every rule's resolved dollar figure is
    unchanged the instant this returns (a $105 weekly 1st place against a $4,950 pot becomes
    a percent rule that still resolves to $105 against that same pot). Returns how many rows
    were converted.

    Raises ValueError, converting nothing, when the effective pot is Decimal("0"): there is no
    real share to freeze a percent rule to, and dividing by a zero pot is exactly the bug this
    guards against, never a silent divide-by-zero. The percent value is quantized to four
    decimal places (PayoutRule.value's own column precision, Numeric(12, 4)) with ROUND_HALF_UP,
    the same rounding a human doing this by hand on a calculator would reach for; the tiny
    quantization remainder is always well under a cent by the time it is resolved back to
    dollars for a pot in the range this pool ever deals with.
    """
    pot = effective_pot(db, pool)
    if pot == 0:
        raise ValueError(
            "There is no pot to scale against yet. Set an entry fee or a pot override first."
        )
    rows = list(db.scalars(select(PayoutRule).where(PayoutRule.pool_id == pool.id)))
    for row in rows:
        rule = Rule(
            scope=row.scope,
            place=row.place,
            mode=row.mode,
            value=Decimal(row.value),
            label=row.label,
        )
        dollars = resolve_rule(rule, pot)
        row.mode = "percent"
        row.value = (dollars / pot * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    db.flush()
    return len(rows)


# The known ladder (DECISIONS.md, "Payout system"): weekly 105/55/25, bowl 250/100/50,
# season_points 600/405/150, season_wins 325/185/110, every one dollar mode. Kept as a module
# level constant, not a magic literal inside load_preset, so a future change to the real
# numbers is a one line diff in an obvious place.
_PRESET_LADDER: dict[str, list[tuple[int, Decimal]]] = {
    "weekly": [(1, Decimal("105")), (2, Decimal("55")), (3, Decimal("25"))],
    "bowl": [(1, Decimal("250")), (2, Decimal("100")), (3, Decimal("50"))],
    "season_points": [(1, Decimal("600")), (2, Decimal("405")), (3, Decimal("150"))],
    "season_wins": [(1, Decimal("325")), (2, Decimal("185")), (3, Decimal("110"))],
}


def load_preset(db: Session, pool: Pool) -> int:
    """Seed all four scopes with the known ladder and set pool.weekly_payout_weeks = 15.

    Always clears every existing PayoutRule row for this pool first, across all four scopes,
    whether or not the pool already has any: this is what makes the route safe to call
    unconditionally rather than needing a separate "already has rules" branch of its own. The
    editor's own confirm() dialog (app/templates/admin/payouts.html) is what actually tells the
    commissioner this replaces whatever is already configured; by the time this function runs
    that confirmation has already happened, so clearing first here is never a surprise, it is
    just what makes the reseed impossible to fail on the (pool_id, scope, place) unique
    constraint or silently duplicate a row. Returns how many rows were created (always 12).
    """
    for row in db.scalars(select(PayoutRule).where(PayoutRule.pool_id == pool.id)):
        db.delete(row)
    db.flush()

    count = 0
    for scope, places in _PRESET_LADDER.items():
        for place, value in places:
            db.add(
                PayoutRule(pool_id=pool.id, scope=scope, place=place, mode="amount", value=value)
            )
            count += 1
    pool.weekly_payout_weeks = 15
    db.flush()
    return count
