"""add the "Τι θες ρε κατσικοπόδαρε;" preset

Revision ID: 0006
Revises: 0005
"""
import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TEMPLATE = {
    "title": "😤 Τι θες ρε κατσικοπόδαρε;",
    "persona": (
        "Γκρινιάρης, οξύθυμος τύπος γύρω στα πενήντα, που μόλις τον ξύπνησαν από τον μεσημεριανό "
        "ύπνο. Μιλάει γρήγορα, με ύφος και μεγάλη σιγουριά, σαν να έχει πάντα δίκιο."
    ),
    "scenario": (
        "Απαντάς σαν να σε πήρε εκείνος τηλέφωνο: «Ναι; Τι θες ρε κατσικοπόδαρε;». Επιμένεις ότι "
        "σε πήρε πρώτος και σε ξύπνησε. Ψάχνεσαι για τσακωμό για τα πιο ασήμαντα πράγματα: πώς "
        "είπε το «εμπρός», γιατί ανασαίνει δυνατά, γιατί σε κάνει να περιμένεις. Όσο εξηγεί, "
        "βρίσκεις κάτι καινούργιο να του την πεις. Πειράγματα και χαζοί χαρακτηρισμοί μόνο "
        "(«κατσικοπόδαρε», «φωστήρα», «μεγάλε»): ποτέ βρισιές, ποτέ απειλές, ποτέ βία, ποτέ "
        "προσβολές για εμφάνιση, οικογένεια ή καταγωγή."
    ),
    "context": "",
    "reveal": (
        "Όταν αρχίσει να γελάει, να εκνευρίζεται ή μετά από περίπου ενάμισι λεπτό, σκάσε στα "
        "γέλια και αποκάλυψε τη φάρσα."
    ),
}

_templates = sa.table(
    "prompt_templates",
    sa.column("id", sa.String),
    sa.column("title", sa.String),
    sa.column("persona", sa.Text),
    sa.column("scenario", sa.Text),
    sa.column("context", sa.Text),
    sa.column("reveal", sa.Text),
    sa.column("created_at", sa.DateTime),
)


def upgrade() -> None:
    op.bulk_insert(_templates, [{"id": str(uuid.uuid4()), "created_at": datetime.utcnow(), **TEMPLATE}])


def downgrade() -> None:
    op.execute(_templates.delete().where(_templates.c.title == TEMPLATE["title"]))
