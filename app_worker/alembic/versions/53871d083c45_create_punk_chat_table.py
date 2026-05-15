"""create punk_chat table

Revision ID: 53871d083c45
Revises: 77c100300b5b
Create Date: 2026-04-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '53871d083c45'
down_revision = '50331b3c39bb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'punk_chat',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_punk_chat_chat_id', 'punk_chat', ['chat_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_punk_chat_chat_id', table_name='punk_chat')
    op.drop_table('punk_chat')
