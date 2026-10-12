"""add clip_start, clip_end, and is_muted to jobs

Revision ID: 005_add_clip_and_mute_to_jobs
Revises: 004_add_menu_message_id
Create Date: 2026-10-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '005_add_clip_and_mute_to_jobs'
down_revision = '004_add_menu_message_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'jobs',
        sa.Column('clip_start', sa.Integer(), nullable=True),
    )
    op.add_column(
        'jobs',
        sa.Column('clip_end', sa.Integer(), nullable=True),
    )
    op.add_column(
        'jobs',
        sa.Column('is_muted', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )


def downgrade() -> None:
    op.drop_column('jobs', 'is_muted')
    op.drop_column('jobs', 'clip_end')
    op.drop_column('jobs', 'clip_start')
