"""add mail tables and notify flag

Revision ID: c2a91e6f7b3d
Revises: b1c4f8a2d5e7
Create Date: 2026-08-15 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2a91e6f7b3d'
down_revision: Union[str, None] = 'b1c4f8a2d5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Transactional email (Phase 7 remediation, see DECISIONS.md). Three independent changes:
    # a single-use, one hour password reset token table; an append-only send log the site
    # admin reads from /site/mail; and one opt-in Pool column for the week-published
    # notification, off by default.
    op.create_table(
        'password_reset_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_password_reset_tokens_user_id'), 'password_reset_tokens', ['user_id']
    )
    op.create_index(
        op.f('ix_password_reset_tokens_token_hash'), 'password_reset_tokens', ['token_hash']
    )

    op.create_table(
        'mail_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('recipient', sa.String(length=255), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('result', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('actor_key', sa.String(length=120), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_mail_log_actor_key'), 'mail_log', ['actor_key'])
    op.create_index(op.f('ix_mail_log_created_at'), 'mail_log', ['created_at'])

    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'notify_week_published', sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.drop_column('notify_week_published')

    op.drop_index(op.f('ix_mail_log_created_at'), table_name='mail_log')
    op.drop_index(op.f('ix_mail_log_actor_key'), table_name='mail_log')
    op.drop_table('mail_log')

    op.drop_index(op.f('ix_password_reset_tokens_token_hash'), table_name='password_reset_tokens')
    op.drop_index(op.f('ix_password_reset_tokens_user_id'), table_name='password_reset_tokens')
    op.drop_table('password_reset_tokens')
