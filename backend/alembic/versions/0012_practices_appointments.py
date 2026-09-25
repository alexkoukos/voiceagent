"""2.0 receptionist: practices, appointments, inbound/web calls

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "practices",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=True, unique=True),
        sa.Column("timezone", sa.String(), nullable=False, server_default="Europe/Athens"),
        sa.Column("language", sa.String(), nullable=False, server_default="el"),
        sa.Column("voice", sa.String(), nullable=False, server_default="Algieba"),
        sa.Column("greeting", sa.Text(), nullable=False, server_default=""),
        sa.Column("phone_numbers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("hours", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("services", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("rules", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("knowledge_base", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("calendar_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("calls", sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=True))
    op.add_column("calls", sa.Column("direction", sa.String(), nullable=False, server_default="outbound"))
    op.add_column("calls", sa.Column("caller_number", sa.String(), nullable=True))
    op.alter_column("calls", "friend_id", nullable=True)
    op.create_table(
        "appointments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=True),
        sa.Column("customer_name", sa.String(), nullable=False),
        sa.Column("customer_phone", sa.String(), nullable=True),
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("service_name", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Enum("booked", "cancelled", name="appointmentstatus"), nullable=False),
        sa.Column("gcal_event_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("appointments")
    sa.Enum(name="appointmentstatus").drop(op.get_bind(), checkfirst=True)
    op.alter_column("calls", "friend_id", nullable=False)
    op.drop_column("calls", "caller_number")
    op.drop_column("calls", "direction")
    op.drop_column("calls", "practice_id")
    op.drop_table("practices")
