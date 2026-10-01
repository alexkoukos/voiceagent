"""Demo requests from the Greek landing page."""

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "demo_leads",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("business_name", sa.Text(), nullable=False),
        sa.Column("business_type", sa.String(length=40), nullable=False),
        sa.Column("contact_method", sa.String(length=10), nullable=False),
        sa.Column("contact_detail", sa.Text(), nullable=False),
        sa.Column("website", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_demo_leads_source_hash", "demo_leads", ["source_hash"])
    op.create_index("ix_demo_leads_created_at", "demo_leads", ["created_at"])


def downgrade():
    op.drop_index("ix_demo_leads_created_at", table_name="demo_leads")
    op.drop_index("ix_demo_leads_source_hash", table_name="demo_leads")
    op.drop_table("demo_leads")
