"""calls.end_reason: why a call didn't connect (no_answer, declined, unreachable, error)

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("end_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "end_reason")
