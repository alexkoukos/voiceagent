"""presets become one free-text description; built-in presets get male voices

The app now has a single textbox per call. Each preset's persona, context and reveal
are folded into its scenario text, so picking a preset fills that one box. Built-in
presets without a voice of their own used to fall back to the female default voice.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

BUILT_IN_VOICES = {
    "🍕 Λάθος παραγγελία": "Puck",
    "📻 Κέρδισες στο ραδιόφωνο": "Fenrir",
    "🦜 Ο γείτονας με τον παπαγάλο": "Charon",
    "🚕 Ξεχάσατε κάτι στο ταξί": "Puck",
    "📋 Έρευνα για κάλτσες": "Charon",
}

_templates = sa.table(
    "prompt_templates",
    sa.column("id", sa.String),
    sa.column("title", sa.String),
    sa.column("persona", sa.Text),
    sa.column("scenario", sa.Text),
    sa.column("context", sa.Text),
    sa.column("reveal", sa.Text),
    sa.column("voice", sa.String),
)


def _fold(persona: str, scenario: str, context: str, reveal: str) -> str:
    parts = [f"Ο ρόλος σου: {persona.strip()}"] if persona and persona.strip() else []
    parts.append(scenario.strip())
    if context and context.strip():
        parts.append(f"Τι ξέρεις για τον φίλο: {context.strip()}")
    if reveal and reveal.strip():
        parts.append(f"Αποκάλυψη: {reveal.strip()}")
    return "\n\n".join(parts)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.select(_templates)).mappings().all()
    for r in rows:
        values = {
            "scenario": _fold(r["persona"], r["scenario"], r["context"], r["reveal"]),
            "persona": "", "context": "", "reveal": "",
        }
        if r["voice"] is None and r["title"] in BUILT_IN_VOICES:
            values["voice"] = BUILT_IN_VOICES[r["title"]]
        conn.execute(_templates.update().where(_templates.c.id == r["id"]).values(**values))


def downgrade() -> None:
    # The folded text stays in `scenario`; only the added voices are undone.
    op.execute(
        _templates.update()
        .where(_templates.c.title.in_(list(BUILT_IN_VOICES)))
        .values(voice=None)
    )
