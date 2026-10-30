"""Drop the friend dialer: friends, saved presets and the call columns only it used.

Old dialer calls stay (status, transcript, recording); they lose the friend and the script.
Back up the database before running this: the downgrade recreates the tables empty.
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

CALL_COLUMNS = ("friend_id", "persona", "scenario", "context", "reveal", "from_own_number")


def upgrade():
    for column in CALL_COLUMNS:
        op.drop_column("calls", column)
    op.drop_table("prompt_templates")
    op.drop_table("friends")


def downgrade():
    op.create_table(
        "friends",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column("reveal", sa.Text(), nullable=False),
        sa.Column("voice", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("calls", sa.Column("friend_id", sa.String(), sa.ForeignKey("friends.id"), nullable=True))
    for column in ("persona", "scenario", "context", "reveal"):
        op.add_column("calls", sa.Column(column, sa.Text(), nullable=False, server_default=""))
    op.add_column("calls", sa.Column("from_own_number", sa.Boolean(), nullable=False, server_default=sa.false()))
