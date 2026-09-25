"""remove the built-in presets; only presets the user saved stay

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

BUILT_IN_TITLES = (
    "🍕 Λάθος παραγγελία",
    "📻 Κέρδισες στο ραδιόφωνο",
    "🦜 Ο γείτονας με τον παπαγάλο",
    "🚕 Ξεχάσατε κάτι στο ταξί",
    "📋 Έρευνα για κάλτσες",
    "😤 Τι θες ρε κατσικοπόδαρε;",
)

_templates = sa.table("prompt_templates", sa.column("title", sa.String))


def upgrade() -> None:
    op.execute(_templates.delete().where(_templates.c.title.in_(BUILT_IN_TITLES)))


def downgrade() -> None:
    # Deleted presets aren't restored; earlier migrations hold their text.
    pass
