from app.config import load_master_prompt
from app.languages import english_name

# Greek calls get a Greek prompt: an English prompt made the Greek sound translated.
# The app sends one free-text description (in `scenario`); persona, context and reveal are
# only filled by older app builds and old calls, and are added when present.
PER_CALL_GREEK = "## Αυτή η κλήση\nΜιλάς με: {name}. Μέγιστη διάρκεια: {max_duration_minutes} λεπτά.\n\n{description}"
PER_CALL_OTHER = (
    "## This call\nLanguage to start in: {language}. You're talking to: {name}. "
    "Maximum length: {max_duration_minutes} minutes.\n\n{description}"
)
LABELS = {
    "el": {"persona": "Ο ρόλος σου", "context": "Τι ξέρεις για τον φίλο", "reveal": "Αποκάλυψη"},
    "other": {"persona": "Your role", "context": "What you know about the friend", "reveal": "Reveal"},
}


def build_call_prompt(
    *,
    friend_name: str,
    scenario: str,
    persona: str = "",
    context: str = "",
    reveal: str = "",
    max_duration_seconds: int,
    language: str = "el",
) -> str:
    greek = language == "el"
    labels = LABELS["el" if greek else "other"]
    parts = [f"{labels['persona']}: {persona.strip()}"] if persona.strip() else []
    parts.append(scenario.strip())
    parts += [f"{labels[k]}: {v.strip()}" for k, v in (("context", context), ("reveal", reveal)) if v.strip()]
    per_call = (PER_CALL_GREEK if greek else PER_CALL_OTHER).format(
        language=english_name(language),
        name=friend_name,
        description="\n\n".join(parts),
        max_duration_minutes=round(max_duration_seconds / 60, 1),
    )
    master = load_master_prompt(language).strip().replace("{language}", english_name(language))
    return f"{master}\n\n{per_call}"
