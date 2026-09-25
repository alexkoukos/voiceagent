from app.config import load_master_prompt

PER_CALL_TEMPLATE = """## Αυτή η κλήση
- Φίλος: {name}
- Ο ρόλος σου: {persona}
- Η φάρσα: {scenario}
- Τι ξέρεις για τον φίλο: {context}
- Αποκάλυψη: {reveal}
- Μέγιστη διάρκεια: {max_duration_minutes} λεπτά"""


def build_call_prompt(
    *,
    friend_name: str,
    persona: str,
    scenario: str,
    context: str,
    reveal: str,
    max_duration_seconds: int,
) -> str:
    per_call = PER_CALL_TEMPLATE.format(
        name=friend_name,
        persona=persona,
        scenario=scenario,
        context=context or "τίποτα ιδιαίτερο",
        reveal=reveal or "κάν' την με φυσικό τρόπο μόλις πετύχει το αστείο",
        max_duration_minutes=round(max_duration_seconds / 60, 1),
    )
    return f"{load_master_prompt().strip()}\n\n{per_call}"
