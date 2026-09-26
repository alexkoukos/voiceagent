"""Changes the business asks for by SMS or by phone with a PIN (OP2).

Revision ID: 0017
Revises: 0016
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("sender", sa.String(), nullable=False, index=True),
        sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("parsed", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("readback", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
    )
    op.add_column("practices", sa.Column("admin_pin_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("practices", "admin_pin_hash")
    op.drop_table("admin_requests")
