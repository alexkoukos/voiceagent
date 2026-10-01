"""Measured per-call cost breakdown."""

from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calls", sa.Column("cost_breakdown", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("calls", "cost_breakdown")
