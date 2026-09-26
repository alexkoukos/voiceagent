"""Patient data requests (OP8), alerts with backup escalation (OP9), cost cap and blocked
numbers (OP10), offboarding (OP7).

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("phone_hash", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=True, index=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("dedupe_key", sa.String(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("acked_at", sa.DateTime(), nullable=True),
        sa.Column("escalated_at", sa.DateTime(), nullable=True),
    )
    op.add_column("practices", sa.Column("monthly_cost_cap_eur", sa.Float(), nullable=True))
    op.add_column("practices", sa.Column("blocked_numbers", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("practices", sa.Column("offboarded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("practices", "offboarded_at")
    op.drop_column("practices", "blocked_numbers")
    op.drop_column("practices", "monthly_cost_cap_eur")
    op.drop_table("alerts")
    op.drop_table("data_requests")
