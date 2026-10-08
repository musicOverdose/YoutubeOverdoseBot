"""add menu_message_id to job_requests

Revision ID: 004_add_menu_message_id
Revises: 003_telegram_migrations_table
Create Date: 2026-10-08 21:15:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '004_add_menu_message_id'
down_revision = '003_telegram_migrations_table'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'job_requests',
        sa.Column('menu_message_id', sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('job_requests', 'menu_message_id')
