"""Revocable demo dashboards, isolated from practice call history."""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "demo_dashboards",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.add_column("calls", sa.Column("demo_dashboard_id", sa.String(),
                                    sa.ForeignKey("demo_dashboards.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_calls_demo_dashboard_id", "calls", ["demo_dashboard_id"])


def downgrade():
    op.drop_index("ix_calls_demo_dashboard_id", table_name="calls")
    op.drop_column("calls", "demo_dashboard_id")
    op.drop_table("demo_dashboards")
