"""Baseline: the whole schema at revision 0033, in one step.

Migrations 0001-0033 were folded into this one (2026-11-05). A database that is already at
0033 runs nothing; a new database gets alembic/schema.sql.
"""

from pathlib import Path

from alembic import op

revision = "0033"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
    # asyncpg runs one statement at a time; the dump has no functions, so «;» ends each one.
    for statement in sql.split(";\n"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("the baseline has no downgrade")
