"""Durable, deduplicated worker telemetry."""

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "call_telemetry_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.String(), sa.ForeignKey("calls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("call_id", "session_id", "sequence", name="uq_call_telemetry_sequence"),
    )
    op.create_index("ix_call_telemetry_call_cursor", "call_telemetry_events", ["call_id", "id"])


def downgrade():
    op.drop_table("call_telemetry_events")
