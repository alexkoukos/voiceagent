"""Onboarding state of a practice: DPA signature (G2), confirmed forwarding (O5) and test
call, and when it went live. Existing practices keep answering: go-live is a record, not a gate.

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("practices", sa.Column("onboarding", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("practices", "onboarding")
