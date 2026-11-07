"""practices.public_name: the name public web pages show (the agent says practices.name)."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("practices", sa.Column("public_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("practices", "public_name")
