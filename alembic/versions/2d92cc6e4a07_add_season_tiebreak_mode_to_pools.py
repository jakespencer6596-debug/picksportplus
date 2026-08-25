"""add season_tiebreak_mode to pools

Revision ID: 2d92cc6e4a07
Revises: c2a91e6f7b3d
Create Date: 2026-08-24 23:10:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2d92cc6e4a07'
down_revision: Union[str, None] = 'c2a91e6f7b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL, so an existing pool needs a server default to back-fill against; dropped once
    # backfilled since every new row goes through the ORM, which always sends an explicit
    # value (matching this table's own weekly_payout_weeks/payout_rounding/payout_tiebreak
    # precedent from the payout system rebuild migration).
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'season_tiebreak_mode',
                sa.String(length=8),
                nullable=False,
                server_default=sa.text("'wins'"),
            )
        )

    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.alter_column('season_tiebreak_mode', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.drop_column('season_tiebreak_mode')
