"""Notification texts: SMS to customers (in the call's language) and emails to the
business (in the business's language)."""

from app.models import Practice

OUTCOME_EL = {
    "booked": "Νέο ραντεβού", "rescheduled": "Αλλαγή ραντεβού", "cancelled": "Ακύρωση ραντεβού",
    "confirmed": "Επιβεβαίωση ραντεβού", "info_given": "Πληροφορίες", "message_taken": "Μήνυμα",
    "transferred": "Μεταφορά σε άνθρωπο", "abandoned": "Έκλεισε χωρίς αποτέλεσμα", "failed": "Αποτυχία",
}
OUTCOME_EN = {
    "booked": "New booking", "rescheduled": "Rescheduled", "cancelled": "Cancelled", "confirmed": "Confirmed",
    "info_given": "Information", "message_taken": "Message", "transferred": "Transferred to a person",
    "abandoned": "Hung up without a result", "failed": "Failed",
}


def outcome_label(outcome: str | None, language: str) -> str:
    labels = OUTCOME_EL if language == "el" else OUTCOME_EN
    return labels.get(outcome or "", outcome or "-")


def _address(practice: Practice) -> str:
    kb = practice.knowledge_base or {}
    for key in ("Διεύθυνση", "διεύθυνση", "Address", "address"):
        if kb.get(key):
            return kb[key]
    return ""


def _phone(practice: Practice) -> str:
    return (practice.phone_numbers or [""])[0]


def customer_sms(practice: Practice, kind: str, appt: dict, language: str, old: dict | None = None) -> str:
    """kind: booked, rescheduled, cancelled. `appt` is booking.describe() output."""
    when = f"{appt['date_spoken']} {appt['time']}"
    address, phone = _address(practice), _phone(practice)
    if language == "el":
        if kind == "booked":
            text = f"{practice.name}: το ραντεβού σας κλείστηκε για {when} ({appt['service']})."
        elif kind == "rescheduled":
            text = f"{practice.name}: το ραντεβού σας άλλαξε σε {when} ({appt['service']})."
        else:
            return f"{practice.name}: το ραντεβού σας για {when} ακυρώθηκε." + (f" Για νέο ραντεβού: {phone}" if phone else "")
        if address:
            text += f" {address}."
        if phone:
            text += f" Για αλλαγή ή ακύρωση: {phone}"
        return text
    if kind == "booked":
        text = f"{practice.name}: your appointment is booked for {when} ({appt['service']})."
    elif kind == "rescheduled":
        text = f"{practice.name}: your appointment moved to {when} ({appt['service']})."
    else:
        return f"{practice.name}: your appointment on {when} is cancelled." + (f" To book again: {phone}" if phone else "")
    if address:
        text += f" {address}."
    if phone:
        text += f" To change or cancel: {phone}"
    return text


def call_email(practice: Practice, call, appt: dict | None, message=None, old_when: str | None = None) -> tuple[str, str]:
    greek = practice.language == "el"
    label = outcome_label(call.outcome, practice.language)
    who = call.caller_number or ("άγνωστος αριθμός" if greek else "unknown number")
    subject = f"[{practice.name}] {label}: {who}"
    lines = []
    if appt:
        a = f"{appt['customer_name']}, {appt['service']}, {appt['date_spoken']} {appt['time']}"
        if appt.get("staff"):
            a += f" ({appt['staff']})"
        lines.append(("Ραντεβού: " if greek else "Appointment: ") + a)
        if old_when:
            lines.append(("Πριν ήταν: " if greek else "Was: ") + old_when)
    if message is not None:
        lines += _message_lines(message, greek)
    if call.summary:
        lines.append("")
        lines.append(("Περίληψη: " if greek else "Summary: ") + call.summary)
    if call.flags:
        lines.append(("Σημάνσεις: " if greek else "Flags: ") + ", ".join(call.flags))
    duration = call.duration_seconds or 0
    lines.append("")
    lines.append((f"Τηλέφωνο: {who} · Διάρκεια: {duration // 60}:{duration % 60:02d}" if greek
                  else f"Phone: {who} · Duration: {duration // 60}:{duration % 60:02d}"))
    return subject, "\n".join(lines)


def _message_lines(m, greek: bool) -> list[str]:
    if greek:
        return [
            f"Μήνυμα από: {m.caller_name or '-'}",
            f"Τηλέφωνο επικοινωνίας: {m.callback_number or '-'}",
            f"Θέμα: {m.reason or '-'}",
            f"Καλύτερη ώρα: {m.best_time or '-'}",
        ] + (["ΕΠΕΙΓΟΝ"] if m.urgent else [])
    return [
        f"Message from: {m.caller_name or '-'}",
        f"Callback number: {m.callback_number or '-'}",
        f"About: {m.reason or '-'}",
        f"Best time: {m.best_time or '-'}",
    ] + (["URGENT"] if m.urgent else [])


def urgent_message_email(practice: Practice, m) -> tuple[str, str]:
    greek = practice.language == "el"
    subject = (f"[{practice.name}] ΕΠΕΙΓΟΝ μήνυμα: {m.callback_number or '-'}" if greek
               else f"[{practice.name}] URGENT message: {m.callback_number or '-'}")
    return subject, "\n".join(_message_lines(m, greek))


def emergency_email(practice: Practice, call) -> tuple[str, str]:
    greek = practice.language == "el"
    if greek:
        return (f"[{practice.name}] ΕΠΕΙΓΟΝ: πιθανό επείγον περιστατικό",
                f"Ο καλών ({call.caller_number or 'άγνωστος αριθμός'}) περιέγραψε κάτι επείγον. "
                "Του είπαμε να καλέσει το 166 ή το 112.")
    return (f"[{practice.name}] URGENT: possible emergency",
            f"The caller ({call.caller_number or 'unknown number'}) described an emergency. They were told to call 112.")


def handoff_unanswered_email(practice: Practice, call, target: str) -> tuple[str, str]:
    greek = practice.language == "el"
    if greek:
        return (f"[{practice.name}] ΕΠΕΙΓΟΝ: αναπάντητη μεταφορά κλήσης",
                f"Ο καλών {call.caller_number or '(άγνωστος αριθμός)'} ζήτησε να μιλήσει με "
                f"{target or 'κάποιον'} και κανείς δεν απάντησε. Καλέστε τον πίσω.")
    return (f"[{practice.name}] URGENT: unanswered handoff",
            f"Caller {call.caller_number or '(unknown number)'} asked for {target or 'someone'} and nobody picked up. "
            "Please call them back.")


def handoff_push(practice: Practice, call, target: str) -> tuple[str, str]:
    if practice.language == "el":
        return ("Κλήση σε αναμονή", f"{call.caller_number or 'Καλών'} ζητά {target or 'να μιλήσει με κάποιον'}. Πατήστε για σύνδεση.")
    return ("Caller waiting", f"{call.caller_number or 'A caller'} wants {target or 'to talk to someone'}. Tap to join.")


def digest_email(practice: Practice, day_spoken: str, outcomes: dict[str, int], tomorrow: list[dict]) -> tuple[str, str]:
    greek = practice.language == "el"
    total = sum(outcomes.values())
    lines = [(f"Κλήσεις σήμερα: {total}" if greek else f"Calls today: {total}")]
    for outcome, n in sorted(outcomes.items(), key=lambda x: -x[1]):
        lines.append(f"- {outcome_label(outcome, practice.language)}: {n}")
    lines.append("")
    lines.append("Ραντεβού αύριο:" if greek else "Tomorrow's appointments:")
    if not tomorrow:
        lines.append("- κανένα" if greek else "- none")
    for a in tomorrow:
        lines.append(f"- {a['time']} {a['customer_name']} ({a['service']})" + (f", {a['staff']}" if a.get("staff") else ""))
    subject = f"[{practice.name}] Σύνοψη ημέρας {day_spoken}" if greek else f"[{practice.name}] Daily summary {day_spoken}"
    return subject, "\n".join(lines)


def monthly_email(practice: Practice, month: str, calls: int, bookings: int, value: float, threshold: int) -> tuple[str, str]:
    greek = practice.language == "el"
    met = bookings >= threshold
    if greek:
        lines = [
            f"{calls} κλήσεις απαντήθηκαν, {bookings} ραντεβού κλείστηκαν"
            + (f", περίπου {value:,.0f}€." if value else "."),
            ("Η εγγύηση των {t} ραντεβού πιάστηκε." if met else
             "Κάτω από {t} ραντεβού: ο μήνας δεν χρεώνεται.").format(t=threshold),
        ]
        return f"[{practice.name}] Αναφορά {month}", "\n".join(lines)
    lines = [
        f"{calls} calls answered, {bookings} bookings" + (f", about €{value:,.0f}." if value else "."),
        (f"The {threshold}-booking guarantee was met." if met else f"Under {threshold} bookings: this month is free."),
    ]
    return f"[{practice.name}] Report {month}", "\n".join(lines)
