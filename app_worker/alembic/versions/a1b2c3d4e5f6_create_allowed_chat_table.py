"""create allowed_chat table

Revision ID: a1b2c3d4e5f6
Revises: 53871d083c45
Create Date: 2026-05-12 00:00:00.000000

"""
import sqlalchemy as sa

from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = '53871d083c45'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'allowed_chat',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_allowed_chat_chat_id', 'allowed_chat', ['chat_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_allowed_chat_chat_id', table_name='allowed_chat')
    op.drop_table('allowed_chat')
