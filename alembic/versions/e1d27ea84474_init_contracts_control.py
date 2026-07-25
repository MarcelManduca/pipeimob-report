"""init_contracts_control

Revision ID: e1d27ea84474
Revises: 
Create Date: 2026-07-24 20:04:15.881681

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1d27ea84474'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create contracts_control_responsibles
    op.create_table(
        'contracts_control_responsibles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('normalized_name', sa.String(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), sa.FetchedValue(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), sa.FetchedValue(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('normalized_name', name='uq_normalized_name'),
        sa.CheckConstraint('length(btrim(name)) > 0', name='chk_responsible_name_not_empty'),
        sa.CheckConstraint('length(btrim(normalized_name)) > 0', name='chk_responsible_normalized_name_not_empty')
    )

    # 2. Create contracts_control_manual_data
    op.create_table(
        'contracts_control_manual_data',
        sa.Column('transaction_id', sa.String(), nullable=False),
        sa.Column('responsible_id', sa.UUID(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(timezone=True), sa.FetchedValue(), nullable=False),
        sa.Column('created_by_sub', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), sa.FetchedValue(), nullable=False),
        sa.Column('updated_by_sub', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['responsible_id'], ['contracts_control_responsibles.id']),
        sa.PrimaryKeyConstraint('transaction_id'),
        sa.CheckConstraint('version >= 1', name='chk_manual_data_version_min')
    )
    op.create_index('idx_manual_data_responsible_id', 'contracts_control_manual_data', ['responsible_id'])
    op.create_index('idx_manual_data_updated_at', 'contracts_control_manual_data', ['updated_at'])

    # 3. Create contracts_control_manual_data_history
    sa_datetime = sa.DateTime(timezone=True)
    op.create_table(
        'contracts_control_manual_data_history',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('transaction_id', sa.String(), nullable=False),
        sa.Column('field_name', sa.String(), nullable=False),
        sa.Column('previous_value', sa.String(), nullable=True),
        sa.Column('new_value', sa.String(), nullable=True),
        sa.Column('previous_version', sa.Integer(), nullable=True),
        sa.Column('new_version', sa.Integer(), nullable=False),
        sa.Column('changed_at', sa_datetime, sa.FetchedValue(), nullable=False),
        sa.Column('changed_by_sub', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_history_transaction_id', 'contracts_control_manual_data_history', ['transaction_id'])
    op.create_index('idx_history_changed_at', 'contracts_control_manual_data_history', ['changed_at'])


def downgrade() -> None:
    # Drop indices and tables in reverse order
    op.drop_index('idx_history_changed_at', table_name='contracts_control_manual_data_history')
    op.drop_index('idx_history_transaction_id', table_name='contracts_control_manual_data_history')
    op.drop_table('contracts_control_manual_data_history')

    op.drop_index('idx_manual_data_updated_at', table_name='contracts_control_manual_data')
    op.drop_index('idx_manual_data_responsible_id', table_name='contracts_control_manual_data')
    op.drop_table('contracts_control_manual_data')

    op.drop_table('contracts_control_responsibles')

