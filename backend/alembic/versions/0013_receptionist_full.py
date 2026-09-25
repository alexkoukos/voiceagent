"""2.0 receptionist: staff, customers, messages, notifications, routing, handoffs, waitlist

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

PRACTICE_COLUMNS = [
    sa.Column("vertical", sa.String(), nullable=False, server_default=""),
    sa.Column("routing_rules", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("departments", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("notifications", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("reminders", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("outbound_number", sa.String(), nullable=True),
    sa.Column("max_concurrent_calls", sa.Integer(), nullable=False, server_default="2"),
    sa.Column("retention_recordings_days", sa.Integer(), nullable=False, server_default="30"),
    sa.Column("retention_transcripts_days", sa.Integer(), nullable=False, server_default="90"),
    sa.Column("avg_booking_value", sa.Float(), nullable=False, server_default="0"),
    sa.Column("guarantee_threshold", sa.Integer(), nullable=False, server_default="10"),
]

CALL_COLUMNS = [
    sa.Column("use_case", sa.String(), nullable=True),
    sa.Column("outcome", sa.String(), nullable=True),
    sa.Column("appointment_id", sa.String(), nullable=True),
    sa.Column("summary", sa.Text(), nullable=True),
    sa.Column("flags", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("cost_estimate", sa.Float(), nullable=True),
    sa.Column("latency_ms_median", sa.Integer(), nullable=True),
    sa.Column("hours_state", sa.String(), nullable=True),
    sa.Column("purpose", sa.String(), nullable=True),
    sa.Column("review", sa.JSON(), nullable=True),
    sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.false()),
]


def upgrade() -> None:
    for c in PRACTICE_COLUMNS:
        op.add_column("practices", c)
    op.create_table(
        "staff",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="staff"),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("service_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("hours", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("calendar_id", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("bookable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("practice_id", "phone"),
    )
    for c in CALL_COLUMNS:
        op.add_column("calls", c)
    op.add_column("calls", sa.Column("customer_id", sa.String(), sa.ForeignKey("customers.id"), nullable=True))
    op.add_column("appointments", sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True))
    op.add_column("appointments", sa.Column("customer_id", sa.String(), sa.ForeignKey("customers.id"), nullable=True))
    op.add_column("appointments", sa.Column("source", sa.String(), nullable=False, server_default="agent"))
    op.add_column("appointments", sa.Column("reminder_status", sa.String(), nullable=False, server_default="none"))
    op.add_column("appointments", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=True),
        sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("caller_name", sa.String(), nullable=False, server_default=""),
        sa.Column("callback_number", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("best_time", sa.String(), nullable=False, server_default=""),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=True, index=True),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending", index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "routing_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False, server_default=""),
        sa.Column("rule", sa.String(), nullable=False, server_default=""),
        sa.Column("path", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "handoffs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("room_name", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default="app"),
        sa.Column("status", sa.String(), nullable=False, server_default="ringing"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "waitlist",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("customer_name", sa.String(), nullable=False, server_default=""),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("staff_id", sa.String(), nullable=True),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("part_of_day", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="waiting"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "devices",
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=True),
        sa.Column("staff_id", sa.String(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("environment", sa.String(), nullable=False, server_default="sandbox"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for t in ("devices", "waitlist", "handoffs", "routing_events", "notifications", "messages"):
        op.drop_table(t)
    for c in ("updated_at", "reminder_status", "source", "customer_id", "staff_id"):
        op.drop_column("appointments", c)
    op.drop_column("calls", "customer_id")
    for c in CALL_COLUMNS:
        op.drop_column("calls", c.name)
    op.drop_table("customers")
    op.drop_table("staff")
    for c in PRACTICE_COLUMNS:
        op.drop_column("practices", c.name)
