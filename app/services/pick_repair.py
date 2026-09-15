"""Audit and repair for the orphaned picks incident.

The bug (see DECISIONS.md, "Orphaned picks", and PICKS-REPAIR-REPORT.md): before this phase,
app.routers.picks._upsert_picks never deleted a Pick row for a game the player later
deselected. A player who saved a valid picks_required-sized entry, then changed their mind
and saved a different valid entry, ended up with the union of both: more than picks_required
rows, sometimes reusing a confidence value across two different games. Under inverse scoring
an orphan on a winning pick costs nothing (score_pick returns 0 for a correct pick), so only
an orphan sitting on a LOSING pick surfaces as an inflated penalty; the rest sit quietly
inflating pick counts and possible counts until they matter.

Two read paths and one write path, all built on one keep rule:

    Keep the picks_required picks with the most recent updated_at, since that is the
    player's latest intent. Ties (the common case: an entire submission shares one
    updated_at, since _upsert_picks stamps a whole batch with a single `now`) resolve first
    to the pick whose confidence value is not duplicated elsewhere in the same week's picks,
    then to the lower game_id, for determinism.

Dropping to exactly picks_required rows can still leave a set that is not a clean
permutation of 1..picks_required (a real, reported case: a player whose rows accumulated
across more than two saves can still have both a duplicate and a gap after the single
required row is dropped). This module never renumbers a kept pick's confidence value to
paper over that: what a player staked on a game is their decision, not this tool's. A kept
set that is still not clean is flagged in the result for the commissioner's attention instead.

Every orphaned Pick row is archived into PickArchive (reason="orphan_repair") before it is
deleted, exactly the pattern app.services.ingest.rebuild_and_reopen_week already uses for a
commissioner's slate rebuild: nothing here is ever destroyed outright.

audit_pool/audit_week are pure reads and never touch the session's pending state.
plan_and_apply_repair does the real work (archive, delete, rescore) directly against the
caller's session; the caller decides whether to commit it for real or roll it back, which is
what lets repair-picks --dry-run report the exact numbers --apply would have committed,
built from the one real code path rather than a second, hand maintained simulation.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, Pick, PickArchive, Pool, User, Week
from app.scoring import GameOutcome, PickInput, score_week
from app.services import payouts as payout_service
from app.services.results import score_week_for_pool

__all__ = [
    "PlayerWeekAudit",
    "OrphanPick",
    "PayoutDelta",
    "PlayerRepairResult",
    "WeekRepairPlan",
    "audit_week",
    "audit_pool",
    "plan_and_apply_repair",
    "ensure_unique_constraint",
]


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


# Read only audit: doctor-picks -----------------------------------------------


@dataclass
class PlayerWeekAudit:
    """One player's pick data for one week, as doctor-picks reports it. Never mutated,
    never written anywhere: a pure snapshot of what is in the database right now."""

    pool_id: int
    pool_name: str
    week_id: int
    week_number: int
    week_label: str
    user_id: int
    display_name: str
    pick_count: int
    picks_required: int
    is_clean_permutation: bool
    duplicate_values: list[int]
    missing_values: list[int]
    off_slate_game_ids: list[int]

    @property
    def is_affected(self) -> bool:
        """True when this row is the kind of corruption the orphaned picks incident causes:
        more (or fewer) rows than picks_required, or a confidence set that is not a clean
        permutation. A pick whose game left the slate is reported but is NOT, on its own,
        "affected": that is ordinary, documented behavior (Phase 3, a picked game leaving the
        slate), not corruption."""
        return self.pick_count != self.picks_required or not self.is_clean_permutation


def audit_week(db: Session, pool: Pool, week: Week) -> list[PlayerWeekAudit]:
    """Every player who has at least one Pick row for this week, audited. Changes nothing."""
    slate_ids = {
        g.id
        for g in db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True)))
    }
    picks_by_user: dict[int, list[Pick]] = {}
    for pick in db.scalars(select(Pick).where(Pick.week_id == week.id)):
        picks_by_user.setdefault(pick.user_id, []).append(pick)
    if not picks_by_user:
        return []

    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(picks_by_user.keys())))}
    n = pool.picks_required
    rows: list[PlayerWeekAudit] = []
    for user_id, picks in picks_by_user.items():
        values = [p.confidence for p in picks]
        counts = Counter(values)
        duplicate_values = sorted(v for v, c in counts.items() if c > 1)
        # Only call out unused values when the count lines up with picks_required, mirroring
        # app.scoring._confidence_errors's own reasoning: otherwise the count mismatch already
        # explains the gap and this would just repeat it.
        missing_values = (
            sorted(v for v in range(1, n + 1) if v not in counts) if len(picks) == n else []
        )
        in_range = all(1 <= v <= n for v in values)
        clean = len(picks) == n and not duplicate_values and not missing_values and in_range
        off_slate = sorted({p.game_id for p in picks if p.game_id not in slate_ids})
        display_name = users[user_id].display_name if user_id in users else f"user {user_id}"
        rows.append(
            PlayerWeekAudit(
                pool_id=pool.id,
                pool_name=pool.name,
                week_id=week.id,
                week_number=week.week_number,
                week_label=week.label,
                user_id=user_id,
                display_name=display_name,
                pick_count=len(picks),
                picks_required=n,
                is_clean_permutation=clean,
                duplicate_values=duplicate_values,
                missing_values=missing_values,
                off_slate_game_ids=off_slate,
            )
        )
    rows.sort(key=lambda r: r.display_name.lower())
    return rows


def audit_pool(db: Session, pool: Pool) -> list[PlayerWeekAudit]:
    """audit_week for every week the pool has ever had, oldest first. Changes nothing."""
    rows: list[PlayerWeekAudit] = []
    for week in db.scalars(
        select(Week).where(Week.pool_id == pool.id).order_by(Week.season_year, Week.week_number)
    ):
        rows.extend(audit_week(db, pool, week))
    return rows


# Repair: repair-picks ---------------------------------------------------------


@dataclass
class OrphanPick:
    """One Pick row the keep rule did not keep, about to be archived and deleted."""

    user_id: int
    display_name: str
    game_id: int
    matchup: str
    picked_team: str
    confidence: int
    updated_at: dt.datetime


@dataclass
class PayoutDelta:
    """A frozen PayoutAward that would read differently after the repair, for one player."""

    user_id: int
    display_name: str
    scope: str
    old_place: int | None
    new_place: int | None
    old_amount: Decimal
    new_amount: Decimal
    already_paid: bool


@dataclass
class PlayerRepairResult:
    user_id: int
    display_name: str
    before_pick_count: int
    after_pick_count: int
    before_points: int
    after_points: int
    before_correct: int
    after_correct: int
    orphans: list[OrphanPick]
    still_not_clean: bool
    remaining_duplicate_values: list[int]
    remaining_missing_values: list[int]


@dataclass
class WeekRepairPlan:
    pool_id: int
    week_id: int
    week_number: int
    week_label: str
    week_status: str
    is_bowl_week: bool
    is_test_week: bool
    players: list[PlayerRepairResult] = field(default_factory=list)
    payout_deltas: list[PayoutDelta] = field(default_factory=list)
    skipped_paid_scopes: list[str] = field(default_factory=list)


def _keep_rule_sort_key(pick: Pick, dup_counts: Counter[int]) -> tuple:
    """Sort ascending by this key and the first picks_required entries are the ones to KEEP.

    Most recent updated_at first; ties broken toward the pick whose confidence value is not
    duplicated elsewhere in this player's week, then toward the lower game_id. See the module
    docstring for why this is the rule and DECISIONS.md, "Orphaned picks", for why it was
    chosen over, for example, always keeping the lowest confidence values.
    """
    duplicated = 1 if dup_counts[pick.confidence] > 1 else 0
    return (-_aware(pick.updated_at).timestamp(), duplicated, pick.game_id)


def _matchup(game: Game | None) -> str:
    if game is None:
        return "an unknown game"
    return f"{game.away_abbr} at {game.home_abbr}"


def _score(picks: list[Pick], outcomes: list[GameOutcome], pool: Pool):
    inputs = [
        PickInput(game_id=p.game_id, picked_team=p.picked_team, confidence=p.confidence)
        for p in picks
    ]
    return score_week(inputs, outcomes, mode=pool.scoring_mode, picks_required=pool.picks_required)


def _payout_paid_for_scope(db: Session, pool: Pool, scope: str, week_id: int | None) -> bool:
    """True if any frozen PayoutAward for this pool/scope/week already has paid_at set: real
    money has already moved for it over Venmo. This is the hard line repair-picks will not
    cross on its own (see plan_and_apply_repair's skip_paid argument)."""
    rows = db.scalars(
        select(payout_service.PayoutAward).where(
            payout_service.PayoutAward.pool_id == pool.id,
            payout_service.PayoutAward.scope == scope,
            payout_service.PayoutAward.week_id == week_id,
        )
    )
    return any(row.paid_at is not None for row in rows)


def _diff_awards(
    db: Session, pool: Pool, scope: str, week: Week | None, old_awards: list
) -> list[PayoutDelta]:
    old_by_user = {a.user_id: a for a in old_awards}
    live = payout_service.project_awards(db, pool, scope, week=week)
    live_by_user = {a.user_id: a for a in live}
    user_ids = set(old_by_user) | set(live_by_user)
    if not user_ids:
        return []
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids)))}
    already_paid = any(a.paid_at is not None for a in old_awards)
    deltas: list[PayoutDelta] = []
    for user_id in user_ids:
        old = old_by_user.get(user_id)
        new = live_by_user.get(user_id)
        old_amount = old.amount if old else Decimal("0")
        new_amount = new.amount if new else Decimal("0")
        old_place = old.place if old else None
        new_place = new.place if new else None
        if old_amount != new_amount or old_place != new_place:
            deltas.append(
                PayoutDelta(
                    user_id=user_id,
                    display_name=(
                        users[user_id].display_name if user_id in users else f"user {user_id}"
                    ),
                    scope=scope,
                    old_place=old_place,
                    new_place=new_place,
                    old_amount=old_amount,
                    new_amount=new_amount,
                    already_paid=already_paid,
                )
            )
    return deltas


def plan_and_apply_repair(
    db: Session,
    pool: Pool,
    *,
    skip_paid: bool = True,
    skip_week_ids: frozenset[int] = frozenset(),
) -> list[WeekRepairPlan]:
    """Compute the repair plan for every affected week in this pool, always actually writing
    it (archive, delete, rescore) against the given session. The caller owns the transaction:
    this function only flushes, it never commits or rolls back. app/cli.py's repair-picks
    calls this exactly once regardless of --dry-run/--apply and only the caller's own
    commit-or-rollback choice decides whether any of it survives, so a dry run is guaranteed
    to report the exact numbers --apply would have committed, with no second, hand written
    simulation that could quietly drift out of sync with the real write path.

    skip_paid: when True (the only mode app/cli.py's repair-picks ever calls with), a week
    whose weekly/bowl PayoutAward already has paid_at set is left completely untouched (no
    picks touched, no rescoring) and reported via skipped_paid_scopes instead. Real money
    already sent over Venmo is the commissioner's problem to resolve with his group, never
    something this tool silently moves out from under (see DECISIONS.md, "Orphaned picks").
    skip_week_ids lets a caller (Phase 9, production) additionally hold back specific weeks
    the commissioner has not yet reviewed, on top of the paid-award check.
    """
    plans: list[WeekRepairPlan] = []
    weeks = list(
        db.scalars(
            select(Week).where(Week.pool_id == pool.id).order_by(Week.season_year, Week.week_number)
        )
    )
    touched_any_week = False

    for week in weeks:
        if week.id in skip_week_ids:
            continue

        picks_by_user: dict[int, list[Pick]] = {}
        for pick in db.scalars(select(Pick).where(Pick.week_id == week.id)):
            picks_by_user.setdefault(pick.user_id, []).append(pick)
        affected = {
            uid: rows for uid, rows in picks_by_user.items() if len(rows) > pool.picks_required
        }
        if not affected:
            continue

        scope = "bowl" if week.is_bowl_week else "weekly"
        weekly_paid = week.status == "scored" and _payout_paid_for_scope(db, pool, scope, week.id)
        if skip_paid and weekly_paid:
            plan = WeekRepairPlan(
                pool_id=pool.id,
                week_id=week.id,
                week_number=week.week_number,
                week_label=week.label,
                week_status=week.status,
                is_bowl_week=week.is_bowl_week,
                is_test_week=week.is_test_week,
                skipped_paid_scopes=[scope],
            )
            plans.append(plan)
            continue

        games = {g.id: g for g in db.scalars(select(Game).where(Game.week_id == week.id))}
        slate = [g for g in games.values() if g.in_slate]
        outcomes = [GameOutcome(game_id=g.id, status=g.status, winner=g.winner) for g in slate]
        users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(affected.keys())))}

        old_weekly_awards = list(
            db.scalars(
                select(payout_service.PayoutAward).where(
                    payout_service.PayoutAward.pool_id == pool.id,
                    payout_service.PayoutAward.scope == scope,
                    payout_service.PayoutAward.week_id == week.id,
                )
            )
        )

        plan = WeekRepairPlan(
            pool_id=pool.id,
            week_id=week.id,
            week_number=week.week_number,
            week_label=week.label,
            week_status=week.status,
            is_bowl_week=week.is_bowl_week,
            is_test_week=week.is_test_week,
        )

        for user_id, picks in affected.items():
            dup_counts = Counter(p.confidence for p in picks)
            ordered = sorted(picks, key=lambda p: _keep_rule_sort_key(p, dup_counts))
            keep = ordered[: pool.picks_required]
            orphaned = ordered[pool.picks_required :]

            before = _score(picks, outcomes, pool)
            after = _score(keep, outcomes, pool)

            keep_counts = Counter(p.confidence for p in keep)
            n = pool.picks_required
            remaining_duplicates = sorted(v for v, c in keep_counts.items() if c > 1)
            remaining_missing = sorted(v for v in range(1, n + 1) if v not in keep_counts)
            in_range = all(1 <= p.confidence <= n for p in keep)
            still_not_clean = bool(remaining_duplicates or remaining_missing) or not in_range

            display_name = users[user_id].display_name if user_id in users else f"user {user_id}"
            orphans = [
                OrphanPick(
                    user_id=user_id,
                    display_name=display_name,
                    game_id=p.game_id,
                    matchup=_matchup(games.get(p.game_id)),
                    picked_team=p.picked_team,
                    confidence=p.confidence,
                    updated_at=p.updated_at,
                )
                for p in orphaned
            ]

            plan.players.append(
                PlayerRepairResult(
                    user_id=user_id,
                    display_name=display_name,
                    before_pick_count=len(picks),
                    after_pick_count=len(keep),
                    before_points=before.points,
                    after_points=after.points,
                    before_correct=before.correct,
                    after_correct=after.correct,
                    orphans=orphans,
                    still_not_clean=still_not_clean,
                    remaining_duplicate_values=remaining_duplicates,
                    remaining_missing_values=remaining_missing,
                )
            )

            for orphan_pick in orphaned:
                db.add(
                    PickArchive(
                        week_id=week.id,
                        user_id=user_id,
                        game_id=orphan_pick.game_id,
                        picked_team=orphan_pick.picked_team,
                        confidence=orphan_pick.confidence,
                        original_submitted_at=orphan_pick.created_at,
                        reason="orphan_repair",
                    )
                )
            for orphan_pick in orphaned:
                db.delete(orphan_pick)

        plan.players.sort(key=lambda r: r.display_name.lower())
        db.flush()
        touched_any_week = True

        # Rescore this week for real, from the now-repaired Pick rows. When the week was
        # already "scored", score_week_for_pool with no actor only refreshes WeekEntry
        # columns; it deliberately never touches the frozen PayoutAward rows (see
        # app/services/results.py), which is exactly what lets this diff old vs. live below
        # without ever silently overwriting a figure the commissioner may have paid out.
        score_week_for_pool(db, pool, week)
        db.flush()

        if week.status == "scored":
            plan.payout_deltas.extend(_diff_awards(db, pool, scope, week, old_weekly_awards))

        plans.append(plan)

    # Season-wide scopes: only meaningful once the season itself is frozen (the bowl week has
    # finished scoring). If nothing above touched any week, nothing season-wide could have
    # changed either, so this whole block is skipped entirely, dry run or not.
    if touched_any_week:
        season_awards = payout_service.season_awards(db, pool)
        for scope in ("season_points", "season_wins"):
            old_awards = season_awards.get(scope, [])
            if not old_awards:
                continue
            if skip_paid and any(a.paid_at is not None for a in old_awards):
                # Reported once, attached to a synthetic plan-less entry is unnecessary: the
                # per-week skipped_paid_scopes already tells the season-scope story implicitly
                # whenever the bowl week itself was skipped. A season scope paid out with no
                # skipped bowl week (an odd, unexpected state) still must never be silently
                # overwritten, so it is simply left out of payout_deltas here, untouched.
                continue
            deltas = _diff_awards(db, pool, scope, None, old_awards)
            if deltas and plans:
                plans[-1].payout_deltas.extend(deltas)

    # The caller decides whether to commit or roll back (see the docstring above): this
    # function only flushes, so the exact same code path produces both an accurate
    # --dry-run (caller rolls back) and the real --apply write (caller commits).
    return plans


CONSTRAINT_NAME = "uq_pick_user_week_confidence"


def ensure_unique_constraint(engine) -> str:
    """Add uq_pick_user_week_confidence directly, once the data underneath is clean, without
    waiting for a fresh `alembic upgrade head` run. The migration that owns this constraint
    (alembic/versions/8f1491439a97_*) already tried once at deploy time and skips itself if
    any (user_id, week_id, confidence) duplicate still exists; repair-picks --apply is what
    actually clears those duplicates, so it is the natural place to try adding the constraint
    again immediately afterward, in the same run that made it safe.

    Postgres gets a real ALTER TABLE ... ADD CONSTRAINT (what the migration itself would have
    written). SQLite has no such statement; a UNIQUE INDEX of the same name enforces the
    identical rule and is what the test suite's in memory SQLite database ends up with.

    Returns a human readable status string: already present, added, or still blocked (with
    the duplicate count) if some other, unrelated corruption remains.
    """
    import sqlalchemy as sa

    inspector = sa.inspect(engine)
    existing_constraints = {uc["name"] for uc in inspector.get_unique_constraints("picks")}
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("picks")}
    if CONSTRAINT_NAME in existing_constraints or CONSTRAINT_NAME in existing_indexes:
        return "already present"

    with engine.connect() as conn:
        violations = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM ("
                "SELECT 1 FROM picks GROUP BY user_id, week_id, confidence HAVING COUNT(*) > 1"
                ") d"
            )
        ).scalar()
        conn.rollback()  # the count above never needs to hold a transaction open
        if violations:
            return f"still blocked: {violations} duplicate (user_id, week_id, confidence) group(s) remain"

    # A fresh, dedicated transaction for the DDL itself: engine.connect() above already
    # auto-began one for the SELECT (SQLAlchemy 2.0 "future" style), and a second explicit
    # conn.begin() on that same connection raises rather than nesting, so this uses its own
    # engine.begin() block instead of trying to reuse the connection above.
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            conn.execute(
                sa.text(
                    f"CREATE UNIQUE INDEX {CONSTRAINT_NAME} ON picks "
                    "(user_id, week_id, confidence)"
                )
            )
        else:
            conn.execute(
                sa.text(
                    f"ALTER TABLE picks ADD CONSTRAINT {CONSTRAINT_NAME} "
                    "UNIQUE (user_id, week_id, confidence)"
                )
            )
    return "added"
