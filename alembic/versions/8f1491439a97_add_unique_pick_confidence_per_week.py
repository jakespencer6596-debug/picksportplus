"""orphaned picks: unique confidence per user per week

Revision ID: 8f1491439a97
Revises: 7f3a9c2e5b1d
Create Date: 2026-09-14 00:00:00.000000

Adds a unique constraint on picks(user_id, week_id, confidence) so two picks in the same
week can never share a confidence value again, the database-level backstop for the
orphaned picks incident (see DECISIONS.md, "Orphaned picks", and PICKS-REPAIR-REPORT.md).

Existing corrupt rows (a player with an orphaned pick left over from _upsert_picks never
deleting a deselected game's row) can violate this constraint before the data repair
(app/cli.py repair-picks) has run. Per the incident plan, this migration must never fail a
deploy over that: it checks for violations first and, if any remain, skips adding the
constraint and prints a clear warning instead of raising. repair-picks --apply itself
attempts to add the constraint again, directly, once it has confirmed the data underneath
is clean (see app/services/pick_repair.py), so the constraint is added the moment it is
safe to add it rather than only at the next fresh migration run.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8f1491439a97"
down_revision: Union[str, None] = "7f3a9c2e5b1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "uq_pick_user_week_confidence"


def _has_duplicate_confidence_values(conn) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM picks GROUP BY user_id, week_id, confidence "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    )
    return result.first() is not None


def upgrade() -> None:
    conn = op.get_bind()
    if _has_duplicate_confidence_values(conn):
        print(
            "WARNING: picks table still has rows sharing (user_id, week_id, confidence). "
            f"Skipping {CONSTRAINT_NAME}; run `python -m app.cli repair-picks --apply` "
            "and re-run this migration (or let repair-picks add the constraint directly) "
            "once the data is clean."
        )
        return
    with op.batch_alter_table("picks", schema=None) as batch_op:
        batch_op.create_unique_constraint(CONSTRAINT_NAME, ["user_id", "week_id", "confidence"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {uc["name"] for uc in inspector.get_unique_constraints("picks")}
    if CONSTRAINT_NAME in existing:
        with op.batch_alter_table("picks", schema=None) as batch_op:
            batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
