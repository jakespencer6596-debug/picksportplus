"""add platform_settings

Revision ID: b1c4f8a2d5e7
Revises: 4efccffeb9cc
Create Date: 2026-08-14 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c4f8a2d5e7'
down_revision: Union[str, None] = '4efccffeb9cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One platform-wide, site-admin-controlled settings row (Phase 5 remediation: provider
    # controls move to site admin). See app/models.py's PlatformSetting docstring for why this
    # is a single-purpose boolean table rather than a generic key-value settings table.
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('espn_only', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('platform_settings')
