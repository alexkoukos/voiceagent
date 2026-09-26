"""Per-practice recording (PRD G7): `recording_enabled` turns egress off for a practice, and
`recording_notice` makes the greeting say the call is recorded. Existing practices keep what
they did (recording on, no notice); practices created through the API start with the notice on.

Revision ID: 0020
Revises: 0019
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("practices", sa.Column("recording_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("practices", sa.Column("recording_notice", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("practices", "recording_notice")
    op.drop_column("practices", "recording_enabled")
