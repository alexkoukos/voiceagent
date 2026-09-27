"""Pilot billing and auditable guarantee drafts; no provider transmission."""
from alembic import op
import sqlalchemy as sa
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("billing_accounts",
        sa.Column("practice_id",sa.String(),sa.ForeignKey("practices.id"),primary_key=True),
        sa.Column("pilot_started_on",sa.Date(),nullable=False),
        sa.Column("monthly_fee",sa.Numeric(10,2),nullable=False))
    op.create_table("billing_drafts",
        sa.Column("id",sa.String(),primary_key=True),
        sa.Column("practice_id",sa.String(),sa.ForeignKey("practices.id"),nullable=False),
        sa.Column("period_start",sa.Date(),nullable=False),sa.Column("period_end",sa.Date(),nullable=False),
        sa.Column("bookings",sa.Integer(),nullable=False),sa.Column("threshold",sa.Integer(),nullable=False),
        sa.Column("amount",sa.Numeric(10,2),nullable=False),sa.Column("status",sa.String(),nullable=False),
        sa.Column("call_ids",sa.JSON(),nullable=False),sa.Column("created_at",sa.DateTime(),nullable=False),
        sa.UniqueConstraint("practice_id","period_start",name="uq_billing_period"))
    op.create_index("ix_billing_drafts_practice_id","billing_drafts",["practice_id"])


def downgrade():
    op.drop_table("billing_drafts")
    op.drop_table("billing_accounts")
