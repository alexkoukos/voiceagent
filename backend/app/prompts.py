from app.config import load_master_prompt

PER_CALL_TEMPLATE = """Friend: {name}
Your role: {persona}
Scenario: {scenario}
Inside jokes / context: {context}
Reveal: {reveal}
Max duration: {max_duration_minutes} minutes"""


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
        context=context or "none",
        reveal=reveal or "improvise a natural reveal once the joke has landed",
        max_duration_minutes=round(max_duration_seconds / 60, 1),
    )
    return f"{load_master_prompt()}\n\n---\n\n{per_call}"
