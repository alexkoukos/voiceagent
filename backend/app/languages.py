"""Which languages a call can be in.

Calls are only in Greek or English. Every call starts in Greek, whatever the number
(the owner's receptionist rule); English is reachable only when the caller asks for it.
Other languages sounded bad, and the model drifted into them on noisy lines.
The agent keeps the call's language for the whole call.
"""

# code -> name in its own language, for the app
LANGUAGES: dict[str, str] = {"el": "Ελληνικά", "en": "English"}
