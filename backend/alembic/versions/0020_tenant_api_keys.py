"""Revocable single-practice admin API keys."""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tenant_api_keys",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True))
    op.create_index("ix_tenant_api_keys_practice_id", "tenant_api_keys", ["practice_id"])


def downgrade():
    op.drop_table("tenant_api_keys")
