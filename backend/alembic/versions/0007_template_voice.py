"""prompt_templates.voice: a preset can pick its own voice; the grumpy preset gets a rough one

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

GRUMPY_TITLE = "😤 Τι θες ρε κατσικοπόδαρε;"
GRUMPY_PERSONA = (
    "Γκρινιάρης, οξύθυμος τύπος γύρω στα πενήντα, που μόλις τον ξύπνησαν από τον μεσημεριανό ύπνο. "
    "Μιλάει δυνατά, κοφτά και εκνευρισμένα, με τραχιά φωνή, σαν να ψάχνεται για καβγά. Δεν κάνει "
    "παύσεις για ευγένειες, διακόπτει και ανεβάζει τον τόνο όσο ο άλλος εξηγεί."
)

_templates = sa.table(
    "prompt_templates",
    sa.column("title", sa.String),
    sa.column("persona", sa.Text),
    sa.column("voice", sa.String),
)


def upgrade() -> None:
    op.add_column("prompt_templates", sa.Column("voice", sa.String(), nullable=True))
    op.execute(
        _templates.update()
        .where(_templates.c.title == GRUMPY_TITLE)
        .values(voice="Algenib", persona=GRUMPY_PERSONA)
    )


def downgrade() -> None:
    op.drop_column("prompt_templates", "voice")
