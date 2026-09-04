"""slate integrity incident: audit trail, pick archive, league chat, lock policy

Revision ID: 7f3a9c2e5b1d
Revises: 4ed79f23298d
Create Date: 2026-09-04 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7f3a9c2e5b1d'
down_revision: Union[str, None] = '4ed79f23298d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same shape as every other NOT NULL pool column with a real default (season_tiebreak_mode,
    # weekly_tiebreak_mode): a server default backfills every existing pool, then is dropped
    # since the ORM always sends an explicit value from here on.
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'lock_policy',
                sa.String(length=24),
                nullable=False,
                server_default=sa.text("'first_kickoff'"),
            )
        )
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.alter_column('lock_policy', server_default=None)

    with op.batch_alter_table('pool_members', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chat_last_viewed_at', sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table('weeks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('midweek_ack_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('midweek_ack_by_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('midweek_ack_note', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('rebuilt_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            'fk_weeks_midweek_ack_by_user_id_users',
            'users',
            ['midweek_ack_by_user_id'],
            ['id'],
            ondelete='SET NULL',
        )

    op.create_table(
        'slate_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('week_id', sa.Integer(), sa.ForeignKey('weeks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('game_id', sa.Integer(), sa.ForeignKey('games.id', ondelete='SET NULL'), nullable=True),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column(
            'actor_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True
        ),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('before', sa.JSON(), nullable=True),
        sa.Column('after', sa.JSON(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index('ix_slate_changes_week_id', 'slate_changes', ['week_id'])
    op.create_index('ix_slate_changes_created_at', 'slate_changes', ['created_at'])

    op.create_table(
        'pick_archives',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('week_id', sa.Integer(), sa.ForeignKey('weeks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('game_id', sa.Integer(), sa.ForeignKey('games.id', ondelete='SET NULL'), nullable=True),
        sa.Column('picked_team', sa.String(length=8), nullable=False),
        sa.Column('confidence', sa.Integer(), nullable=False),
        sa.Column('original_submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason', sa.String(length=32), nullable=False, server_default=sa.text("'rebuild'")),
        sa.Column(
            'archived_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index('ix_pick_archives_week_id', 'pick_archives', ['week_id'])
    op.create_index('ix_pick_archives_user_id', 'pick_archives', ['user_id'])

    op.create_table(
        'league_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('pool_id', sa.Integer(), sa.ForeignKey('pools.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('pinned', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_league_messages_pool_id', 'league_messages', ['pool_id'])
    op.create_index('ix_league_messages_user_id', 'league_messages', ['user_id'])
    op.create_index('ix_league_messages_created_at', 'league_messages', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_league_messages_created_at', table_name='league_messages')
    op.drop_index('ix_league_messages_user_id', table_name='league_messages')
    op.drop_index('ix_league_messages_pool_id', table_name='league_messages')
    op.drop_table('league_messages')

    op.drop_index('ix_pick_archives_user_id', table_name='pick_archives')
    op.drop_index('ix_pick_archives_week_id', table_name='pick_archives')
    op.drop_table('pick_archives')

    op.drop_index('ix_slate_changes_created_at', table_name='slate_changes')
    op.drop_index('ix_slate_changes_week_id', table_name='slate_changes')
    op.drop_table('slate_changes')

    with op.batch_alter_table('weeks', schema=None) as batch_op:
        batch_op.drop_constraint('fk_weeks_midweek_ack_by_user_id_users', type_='foreignkey')
        batch_op.drop_column('rebuilt_at')
        batch_op.drop_column('midweek_ack_note')
        batch_op.drop_column('midweek_ack_by_user_id')
        batch_op.drop_column('midweek_ack_at')

    with op.batch_alter_table('pool_members', schema=None) as batch_op:
        batch_op.drop_column('chat_last_viewed_at')

    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.drop_column('lock_policy')
