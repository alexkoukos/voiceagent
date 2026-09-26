from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.booking import WEEKDAY_EL, WEEKDAY_EN, WEEKDAY_KEYS, say_date
from app.config import load_master_prompt, load_receptionist_prompt
from app.languages import english_name

# Greek calls get a Greek prompt: an English prompt made the Greek sound translated.
# The app sends one free-text description (in `scenario`); persona, context and reveal are
# only filled by older app builds and old calls, and are added when present.
PER_CALL_GREEK = "## Αυτή η κλήση\nΜιλάς με: {name}. Μέγιστη διάρκεια: {max_duration_minutes} λεπτά.\n\n{description}"
PER_CALL_OTHER = (
    "## This call\nLanguage: {language}. You're talking to: {name}. "
    "Maximum length: {max_duration_minutes} minutes.\n\n{description}"
)
LABELS = {
    "el": {"persona": "Ο ρόλος σου", "context": "Τι ξέρεις για τον άνθρωπο", "reveal": "Αποκάλυψη"},
    "other": {"persona": "Your role", "context": "What you know about them", "reveal": "Reveal"},
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


RECEPTIONIST_LABELS = {
    "el": {
        "header": "## Στοιχεία της επιχείρησης", "now": "Τώρα είναι", "hours": "Ωράριο",
        "closed": "κλειστά", "services": "Υπηρεσίες (id: όνομα, διάρκεια, τιμή)", "info": "Πληροφορίες",
        "caller": "Αριθμός του πελάτη", "unknown": "άγνωστος (ρώτα τον αν χρειαστεί)", "minutes": "λεπτά",
        "state": {"open": "Η επιχείρηση είναι ΑΝΟΙΧΤΗ τώρα.", "break": "Η επιχείρηση κάνει ΔΙΑΛΕΙΜΜΑ τώρα.",
                  "closed": "Η επιχείρηση είναι ΚΛΕΙΣΤΗ τώρα."},
        "opens": "Ανοίγει ξανά", "staff": "Προσωπικό (όνομα, ρόλος, υπηρεσίες)", "all": "όλες",
        "departments": "Τμήματα", "known": "Γνωστός πελάτης", "appts": "Επόμενα ραντεβού του",
        "purpose": "## Σκοπός αυτής της κλήσης",
        "closures": "Κλειστά και άδειες (μην κλείνεις ραντεβού αυτές τις μέρες)", "whole": "όλη η επιχείρηση",
        "until": "έως",
        "roles": {"doctor": "γιατρός", "secretary": "γραμματεία", "owner": "ιδιοκτήτης", "staff": "προσωπικό"},
    },
    "other": {
        "header": "## Business details", "now": "It is now", "hours": "Opening hours",
        "closed": "closed", "services": "Services (id: name, duration, price)", "info": "Information",
        "caller": "Caller's number", "unknown": "unknown (ask them if needed)", "minutes": "min",
        "state": {"open": "The business is OPEN now.", "break": "The business is on a BREAK now.",
                  "closed": "The business is CLOSED now."},
        "opens": "Opens again", "staff": "Staff (name, role, services)", "all": "all",
        "departments": "Departments", "known": "Known customer", "appts": "Their upcoming appointments",
        "purpose": "## Purpose of this call",
        "closures": "Closures and leave (no bookings on these days)", "whole": "whole business",
        "until": "to",
        "roles": {"doctor": "doctor", "secretary": "reception", "owner": "owner", "staff": "staff"},
    },
}


def build_receptionist_prompt(
    practice,
    *,
    now: datetime,
    caller_number: str | None,
    language: str,
    hours_state: str = "open",
    next_open: datetime | None = None,
    customer_name: str | None = None,
    upcoming: list[dict] | None = None,
    staff: list | None = None,
    purpose: str | None = None,
) -> str:
    """Master receptionist prompt + this business's hours, services, staff and knowledge base."""
    greek = language == "el"
    lb = RECEPTIONIST_LABELS["el" if greek else "other"]
    local = now.astimezone(ZoneInfo(practice.timezone))
    weekdays = WEEKDAY_EL if greek else WEEKDAY_EN
    hours = []
    for i, key in enumerate(WEEKDAY_KEYS):
        spans = (practice.hours or {}).get(key) or []
        hours.append(f"- {weekdays[i]}: " + (", ".join(f"{a}-{b}" for a, b in spans) or lb["closed"]))
    services = []
    for s in practice.services or []:
        line = f"- {s['id']}: {s.get('name', s['id'])}, {s['duration_minutes']} {lb['minutes']}"
        if s.get("price"):
            line += f", {s['price']}"
        services.append(line)
    state = lb["state"].get(hours_state, "")
    if hours_state != "open" and next_open:
        state += f" {lb['opens']}: {say_date(next_open.date(), language)} {next_open.strftime('%H:%M')}."
    parts = [
        lb["header"],
        f"{lb['now']}: {say_date(local.date(), language)}, {local.strftime('%H:%M')}. {state}",
        f"{lb['caller']}: {caller_number or lb['unknown']}",
    ]
    if customer_name:
        known = f"{lb['known']}: {customer_name}."
        if upcoming:
            known += f" {lb['appts']}: " + "; ".join(
                f"{a['date_spoken']} {a['time']}, {a['service']} (appointment_id {a['appointment_id']})" for a in upcoming
            )
        parts.append(known)
    parts.append(f"{lb['hours']}:\n" + "\n".join(hours))
    names = {p.id: p.name for p in staff or []}
    closures = [
        f"- {say_date(date.fromisoformat(c['from']), language)} {lb['until']} "
        f"{say_date(date.fromisoformat(c['to']), language)}: "
        + (names.get(c.get("staff_id"), "?") if c.get("staff_id") else lb["whole"])
        + (f" ({c['reason']})" if c.get("reason") else "")
        for c in sorted((practice.rules or {}).get("closures") or [], key=lambda c: c["from"])
        if c["to"] >= local.date().isoformat()
    ]
    if closures:
        parts.append(f"{lb['closures']}:\n" + "\n".join(closures))
    parts.append(f"{lb['services']}:\n" + "\n".join(services))
    if staff:
        rows = []
        for p in staff:
            role = lb["roles"].get(p.role, p.role)
            svc = ", ".join(p.service_ids) if p.service_ids else lb["all"]
            rows.append(f"- {p.name} ({role}; {svc})")
        parts.append(f"{lb['staff']}:\n" + "\n".join(rows))
    if practice.departments:
        parts.append(f"{lb['departments']}:\n" + "\n".join(
            f"- {d.get('name', d['id'])}: {', '.join(d.get('service_ids', []))}" for d in practice.departments
        ))
    info = [f"- {k}: {v}" for k, v in (practice.knowledge_base or {}).items()]
    if info:
        parts.append(f"{lb['info']}:\n" + "\n".join(info))
    master = load_receptionist_prompt(language).strip().replace("{practice_name}", practice.name)
    out = master + "\n\n" + "\n\n".join(parts)
    if purpose:
        out += f"\n\n{lb['purpose']}\n{purpose}"
    return out


def default_greeting(practice, *, language: str) -> str:
    """Opening line in the practice's language. A custom greeting replaces it."""
    if practice.greeting.strip():
        return practice.greeting.strip()
    if language == "el":
        return "Είμαι ψηφιακός βοηθός, πώς μπορώ να σας βοηθήσω; Για αγγλικά, πείτε «English»."
    return "I'm a digital assistant, how can I help you?"
