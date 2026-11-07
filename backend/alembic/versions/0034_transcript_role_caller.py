"""The caller's transcript lines are role "caller" (was "friend", from the first prototype)."""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE transcriptrole RENAME VALUE 'friend' TO 'caller'")


def downgrade() -> None:
    op.execute("ALTER TYPE transcriptrole RENAME VALUE 'caller' TO 'friend'")
