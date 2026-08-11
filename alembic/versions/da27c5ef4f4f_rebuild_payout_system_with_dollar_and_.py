"""rebuild payout system with dollar and percent modes

Revision ID: da27c5ef4f4f
Revises: e58c1a9f7d23
Create Date: 2026-08-11 13:15:25.449562

Replaces the earlier, simpler payout_rules table (float amount, three scopes: weekly, bowl,
season) with the four scope, dollar-or-percent shape the payout system rebuild needs, and adds
payout_awards, the frozen per-week/per-player snapshot table. See DECISIONS.md, "Payout system",
Phase 0: production has zero real payout_rules rows, so this is a clean drop-and-recreate rather
than an in-place column migration of old rows into the new shape. downgrade() recreates the old
table's exact original shape so the migration is still fully reversible either direction, it
just does not attempt to carry old rows across the shape change.

Also converts pools.entry_fee from Float to Numeric(10, 2) and adds pools.pot_override,
pools.weekly_payout_weeks, pools.payout_rounding, and pools.payout_tiebreak: every payout-facing
money field on Pool moves to Decimal/Numeric with this pass (see DECISIONS.md).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'da27c5ef4f4f'
down_revision: Union[str, None] = 'e58c1a9f7d23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # payout_rules: drop the old float/three-scope shape outright and recreate in the new
    # dollar-or-percent, four-scope shape. See the module docstring above for why this is a
    # clean cut rather than an in-place column migration.
    op.drop_table('payout_rules')
    op.create_table(
        'payout_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('place', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=8), nullable=False),
        sa.Column('value', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('label', sa.String(length=60), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['pool_id'], ['pools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pool_id', 'scope', 'place', name='uq_payout_rule_pool_scope_place'),
        sa.CheckConstraint('place >= 1', name='ck_payout_rule_place_positive'),
        sa.CheckConstraint('value >= 0', name='ck_payout_rule_value_non_negative'),
    )
    with op.batch_alter_table('payout_rules', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payout_rules_pool_id'), ['pool_id'], unique=False)

    # payout_awards: brand new, the frozen snapshot table.
    op.create_table(
        'payout_awards',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('week_id', sa.Integer(), nullable=True),
        sa.Column('place', sa.Integer(), nullable=False),
        sa.Column('tied_with', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('pot_at_award', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('rule_mode', sa.String(length=8), nullable=False),
        sa.Column('rule_value', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('awarded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('recalculated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recalculated_by_user_id', sa.Integer(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_marked_by_user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['paid_marked_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['pool_id'], ['pools.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recalculated_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['week_id'], ['weeks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'pool_id', 'scope', 'week_id', 'user_id', name='uq_payout_award_pool_scope_week_user'
        ),
    )
    with op.batch_alter_table('payout_awards', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payout_awards_pool_id'), ['pool_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payout_awards_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_payout_awards_week_id'), ['week_id'], unique=False)

    # pools: new payout settings. The three NOT NULL additions need a server default to
    # backfill any existing row (matching this table's own prior payment_required_to_pick
    # precedent), then the default is dropped since every new row goes through the ORM, which
    # always sends an explicit value.
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pot_override', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(
            sa.Column(
                'weekly_payout_weeks', sa.Integer(), nullable=False, server_default=sa.text('15')
            )
        )
        batch_op.add_column(
            sa.Column(
                'payout_rounding',
                sa.String(length=8),
                nullable=False,
                server_default=sa.text("'dollar'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                'payout_tiebreak',
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'earliest_submit'"),
            )
        )
        batch_op.alter_column(
            'entry_fee',
            existing_type=sa.FLOAT(),
            type_=sa.Numeric(precision=10, scale=2),
            existing_nullable=True,
        )

    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.alter_column('weekly_payout_weeks', server_default=None)
        batch_op.alter_column('payout_rounding', server_default=None)
        batch_op.alter_column('payout_tiebreak', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('pools', schema=None) as batch_op:
        batch_op.alter_column(
            'entry_fee',
            existing_type=sa.Numeric(precision=10, scale=2),
            type_=sa.FLOAT(),
            existing_nullable=True,
        )
        batch_op.drop_column('payout_tiebreak')
        batch_op.drop_column('payout_rounding')
        batch_op.drop_column('weekly_payout_weeks')
        batch_op.drop_column('pot_override')

    with op.batch_alter_table('payout_awards', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payout_awards_week_id'))
        batch_op.drop_index(batch_op.f('ix_payout_awards_user_id'))
        batch_op.drop_index(batch_op.f('ix_payout_awards_pool_id'))
    op.drop_table('payout_awards')

    with op.batch_alter_table('payout_rules', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_payout_rules_pool_id'))
    op.drop_table('payout_rules')
    op.create_table(
        'payout_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pool_id', sa.Integer(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('place', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(['pool_id'], ['pools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('payout_rules', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_payout_rules_pool_id'), ['pool_id'], unique=False)
