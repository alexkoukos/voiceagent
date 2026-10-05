"""Allow explicitly non-expiring demo links; existing expirations stay unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("demo_dashboards", "expires_at", existing_type=sa.DateTime(), nullable=True)


def downgrade():
    # A rollback cannot retain unbounded links under the previous schema.
    op.execute("UPDATE demo_dashboards SET expires_at = CURRENT_TIMESTAMP WHERE expires_at IS NULL")
    op.alter_column("demo_dashboards", "expires_at", existing_type=sa.DateTime(), nullable=False)
