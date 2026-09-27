"""Tenant-scoped read-only calendar feeds."""
from alembic import op
import sqlalchemy as sa
revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("calendar_feeds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False),
        sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["practice_id", "staff_id"], ["staff.practice_id", "staff.id"],
                                name="fk_calendar_feeds_staff_id_tenant"))
    op.create_index("ix_calendar_feeds_practice_id", "calendar_feeds", ["practice_id"])


def downgrade():
    op.drop_table("calendar_feeds")
