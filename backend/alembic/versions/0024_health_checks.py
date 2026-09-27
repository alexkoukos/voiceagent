"""Tenant-scoped synthetic call monitor results."""
from alembic import op
import sqlalchemy as sa
revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("health_checks",
        sa.Column("id",sa.String(),primary_key=True),
        sa.Column("practice_id",sa.String(),sa.ForeignKey("practices.id"),nullable=False),
        sa.Column("kind",sa.String(),nullable=False),sa.Column("number",sa.String(),nullable=True),
        sa.Column("success",sa.Boolean(),nullable=False),sa.Column("detail",sa.String(),nullable=False),
        sa.Column("created_at",sa.DateTime(),nullable=False))
    op.create_index("ix_health_checks_practice_id","health_checks",["practice_id"])
    op.create_index("ix_health_checks_created_at","health_checks",["created_at"])


def downgrade():
    op.drop_table("health_checks")
