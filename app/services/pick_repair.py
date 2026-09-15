"""Audit and repair for the orphaned picks incident.

The bug (see DECISIONS.md, "Orphaned picks", and PICKS-REPAIR-REPORT.md): before this phase,
app.routers.picks._upsert_picks never deleted a Pick row for a game the player later
deselected. A player who saved a valid picks_required-sized entry, then changed their mind
and saved a different valid entry, ended up with the union of both: more than picks_required
rows, sometimes reusing a confidence value across two different games. Under inverse scoring
an orphan on a winning pick costs nothing (score_pick returns 0 for a correct pick), so only
an orphan sitting on a LOSING pick surfaces as an inflated penalty; the rest sit quietly
inflating pick counts and possible counts until they matter.

This module starts with the read only audit (audit_pool/audit_week, used by the doctor-picks
CLI command and the commissioner dashboard warning). The repair side (the keep rule,
plan_and_apply_repair, the repair-picks CLI command) lands in a later commit once the audit
above has proven out what it finds in real data; see DECISIONS.md, "Orphaned picks", for the
full incident writeup.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, Pick, Pool, User, Week

__all__ = [
    "PlayerWeekAudit",
    "audit_week",
    "audit_pool",
]


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
