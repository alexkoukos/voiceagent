"""calls.flow: the conversation state machine's per-call state."""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calls", sa.Column("flow", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("calls", "flow")
