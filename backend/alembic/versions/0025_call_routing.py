"""Per-practice carrier forwarding configuration."""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("practices", sa.Column("call_routing", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("practices", "call_routing")
