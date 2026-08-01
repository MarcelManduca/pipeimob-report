"""add_import_previews

Revision ID: b5e1c4df8211
Revises: e1d27ea84474
Create Date: 2026-07-27 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5e1c4df8211'
down_revision: Union[str, Sequence[str], None] = 'e1d27ea84474'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create contracts_control_import_previews
    op.create_table(
        'contracts_control_import_previews',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('source_filename', sa.String(), nullable=False),
        sa.Column('source_format', sa.String(), nullable=False),
        sa.Column('parser_version', sa.String(), nullable=False),
        sa.Column('created_by_sub', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column('source_hash', sa.String(), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 2. Create contracts_control_import_preview_items
    op.create_table(
        'contracts_control_import_preview_items',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('preview_id', sa.UUID(), nullable=False),
        sa.Column('aba', sa.String(), nullable=False),
        sa.Column('linha', sa.Integer(), nullable=False),
        sa.Column('codigo_imovel', sa.String(), nullable=True),
        sa.Column('nome_imovel', sa.String(), nullable=True),
        sa.Column('responsavel_planilha', sa.String(), nullable=True),
        sa.Column('responsavel_atual_secretaria', sa.String(), nullable=True),
        sa.Column('transaction_id', sa.String(), nullable=True),
        sa.Column('versao_manual_atual', sa.Integer(), nullable=True),
        sa.Column('decisao_proposta', sa.String(), nullable=False),
        sa.Column('motivo', sa.String(), nullable=True),
        sa.Column('source_occurrences', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['preview_id'], ['contracts_control_import_previews.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_preview_item_preview_id', 'contracts_control_import_preview_items', ['preview_id'])
    op.create_index('idx_preview_item_decisao_proposta', 'contracts_control_import_preview_items', ['decisao_proposta'])
    op.create_index('idx_preview_item_codigo_imovel', 'contracts_control_import_preview_items', ['codigo_imovel'])


def downgrade() -> None:
    op.drop_index('idx_preview_item_codigo_imovel', table_name='contracts_control_import_preview_items')
    op.drop_index('idx_preview_item_decisao_proposta', table_name='contracts_control_import_preview_items')
    op.drop_index('idx_preview_item_preview_id', table_name='contracts_control_import_preview_items')
    op.drop_table('contracts_control_import_preview_items')
    op.drop_table('contracts_control_import_previews')
