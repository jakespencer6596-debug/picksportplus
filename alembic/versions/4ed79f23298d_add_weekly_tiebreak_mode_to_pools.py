"""add weekly_tiebreak_mode to pools

Revision ID: 4ed79f23298d
Revises: 2d92cc6e4a07
Create Date: 2026-08-28 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4ed79f23298d'
down_revision: Union[str, None] = '2d92cc6e4a07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same shape as 2d92cc6e4a07 (season_tiebreak_mode): NOT NULL needs a server default to
    # back-fill an existing pool, dropped once backfilled since every new row goes through the
    # ORM, which always sends an explicit value.
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'weekly_tiebreak_mode',
                sa.String(length=8),
                nullable=False,
                server_default=sa.text("'wins'"),
            )
        )

    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.alter_column('weekly_tiebreak_mode', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.drop_column('weekly_tiebreak_mode')
