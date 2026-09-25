from app.config import load_master_prompt
from app.languages import english_name

# Greek calls get a Greek prompt: an English prompt made the Greek sound translated.
PER_CALL_GREEK = """## Αυτή η κλήση
- Φίλος: {name}
- Ο ρόλος σου: {persona}
- Η φάρσα: {scenario}
- Τι ξέρεις για τον φίλο: {context}
- Αποκάλυψη: {reveal}
- Μέγιστη διάρκεια: {max_duration_minutes} λεπτά"""

PER_CALL_OTHER = """## This call
- Language to start in: {language}
- Friend: {name}
- Your role: {persona}
- The joke: {scenario}
- What you know about the friend: {context}
- Reveal: {reveal}
- Maximum length: {max_duration_minutes} minutes"""


def build_call_prompt(
    *,
    friend_name: str,
    persona: str,
    scenario: str,
    context: str,
    reveal: str,
    max_duration_seconds: int,
    language: str = "el",
) -> str:
    greek = language == "el"
    per_call = (PER_CALL_GREEK if greek else PER_CALL_OTHER).format(
        language=english_name(language),
        name=friend_name,
        persona=persona,
        scenario=scenario,
        context=context or ("τίποτα ιδιαίτερο" if greek else "nothing in particular"),
        reveal=reveal or (
            "κάν' την με φυσικό τρόπο μόλις πετύχει το αστείο" if greek
            else "do it naturally once the joke has landed"
        ),
        max_duration_minutes=round(max_duration_seconds / 60, 1),
    )
    master = load_master_prompt(language).strip().replace("{language}", english_name(language))
    return f"{master}\n\n{per_call}"
