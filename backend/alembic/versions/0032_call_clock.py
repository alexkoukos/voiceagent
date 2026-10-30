"""calls.clock_start: eval calls run on a fixed clock."""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calls", sa.Column("clock_start", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("calls", "clock_start")
