"""queued/cancelled statuses, delete_requested, seed templates

Revision ID: 0002
Revises: 0001
"""
import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SEED_TEMPLATES = [
    {
        "title": "Wrong order",
        "persona": "A cheerful, slightly stressed delivery person from a pizza place",
        "scenario": (
            "You are calling to confirm an order the friend never made: an absurd number of "
            "pizzas with ridiculous toppings. Stay polite and confused, insist the order is "
            "under their name, and get funnier as they object. Never ask for an address, "
            "payment or any personal data."
        ),
        "context": "",
        "reveal": "After about a minute, or as soon as they get properly annoyed, reveal the prank.",
    },
    {
        "title": "Fake call from the university",
        "persona": "A formal but warm secretary from a fictional university administration office",
        "scenario": (
            "Tell the friend their name came up on a list for the 'Best Excuse for Missing a "
            "Lecture' award, and they must read out their best excuse to qualify. Stay serious "
            "and bureaucratic. Do not name a real university or a real person, and do not "
            "mention grades, fees or anything that could actually worry them."
        ),
        "context": "",
        "reveal": "Once they have given an excuse and laughed or played along, reveal the prank.",
    },
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE callstatus ADD VALUE IF NOT EXISTS 'queued'")
            op.execute("ALTER TYPE callstatus ADD VALUE IF NOT EXISTS 'cancelled'")

    op.add_column(
        "calls",
        sa.Column("delete_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    templates = sa.table(
        "prompt_templates",
        sa.column("id", sa.String),
        sa.column("title", sa.String),
        sa.column("persona", sa.Text),
        sa.column("scenario", sa.Text),
        sa.column("context", sa.Text),
        sa.column("reveal", sa.Text),
        sa.column("created_at", sa.DateTime),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        templates,
        [{"id": str(uuid.uuid4()), "created_at": now, **t} for t in SEED_TEMPLATES],
    )


def downgrade() -> None:
    op.execute("DELETE FROM prompt_templates WHERE title IN ('Wrong order', 'Fake call from the university')")
    op.drop_column("calls", "delete_requested")
