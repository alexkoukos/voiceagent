"""Import evidence, weekly Google refresh and publish freeze.

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("practices", sa.Column("publish_frozen", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("practices", sa.Column("google_place_id", sa.String(), nullable=True))
    op.add_column("practices", sa.Column("google_refreshed_at", sa.DateTime(), nullable=True))
    op.create_table("imports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("practice_id", sa.String(), sa.ForeignKey("practices.id"), nullable=False, index=True),
        sa.Column("version_id", sa.String(), sa.ForeignKey("config_versions.id"), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("extracted", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("imports")
    for name in ("google_refreshed_at", "google_place_id", "publish_frozen"):
        op.drop_column("practices", name)
