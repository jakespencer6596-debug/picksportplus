"""add co_commissioner_invited_at to pool_members

Revision ID: 947d228f154d
Revises: 176d4f464857
Create Date: 2026-08-10 15:20:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '947d228f154d'
down_revision: Union[str, None] = '176d4f464857'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no backfill needed: null means "no co-commissioner invite pending", which is
    # exactly right for every existing row (Post-launch fixes: co-commissioner self-service
    # invites with confirmation). Set only by POST /admin/members/{id}/co-commissioner/invite,
    # cleared by accept or decline or by any direct role_in_pool change through
    # POST /admin/members/{id}/role. See app/models.py, PoolMember.co_commissioner_invited_at,
    # and DECISIONS.md.
    with op.batch_alter_table('pool_members', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('co_commissioner_invited_at', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('pool_members', schema=None) as batch_op:
        batch_op.drop_column('co_commissioner_invited_at')
