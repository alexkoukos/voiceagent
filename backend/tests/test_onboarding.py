"""O1, O2, O5: Google hours, price list merge, forwarding codes."""

from app.onboarding import _slug, forwarding_codes, hours_from_google, merge_services


def test_google_hours_split_days_and_24h():
    opening = {"periods": [
        {"open": {"day": 1, "hour": 9, "minute": 0}, "close": {"day": 1, "hour": 14, "minute": 0}},
        {"open": {"day": 1, "hour": 17, "minute": 30}, "close": {"day": 1, "hour": 21, "minute": 0}},
        {"open": {"day": 5, "hour": 20, "minute": 0}, "close": {"day": 6, "hour": 2, "minute": 0}},
        {"open": {"day": 0, "hour": 0, "minute": 0}},
    ]}
    h = hours_from_google(opening)
    assert h["mon"] == [["09:00", "14:00"], ["17:30", "21:00"]]
    assert h["fri"] == [["20:00", "23:59"]]
    assert h["sun"] == [["00:00", "23:59"]]
    assert h["tue"] == []


def test_price_list_merge_keeps_ids_and_flags_gaps():
    current = [{"id": "check", "name": "Καθαρισμός δοντιών", "duration_minutes": 30, "price": "45€"}]
    extracted = [
        {"name": "καθαρισμός  δοντιών", "price": "50€", "duration_minutes": None, "confidence": 0.95},
        {"name": "Λεύκανση", "price": "250€", "duration_minutes": 60, "confidence": 0.9},
        {"name": "Σφράγισμα", "price": None, "duration_minutes": 40, "confidence": 0.9},
        {"name": "Θολό", "price": "20€", "duration_minutes": 20, "confidence": 0.4},
        {"name": "Λεύκανση", "price": "250€", "duration_minutes": 60, "confidence": 0.9},
    ]
    services, check = merge_services(current, extracted)
    assert services[0] == {"id": "check", "name": "Καθαρισμός δοντιών", "duration_minutes": 30, "price": "50€"}
    assert [s["name"] for s in services] == ["Καθαρισμός δοντιών", "Λεύκανση", "Σφράγισμα", "Θολό"]
    assert services[1]["id"] == "leukansi" and services[1]["duration_minutes"] == 60
    assert check == ["Σφράγισμα", "Θολό"]


def test_slugs_are_ascii_and_unique():
    used = {"check"}
    assert _slug("Ψυχρή Θεραπεία", used) == "psychri-therapeia"
    assert _slug("Ψυχρή Θεραπεία", used) == "psychri-therapeia-2"
    assert _slug("!!!", used) == "service"


def test_forwarding_codes():
    backup = forwarding_codes("+302100000000", "backup")
    assert [c["code"] for c in backup] == ["**61*+302100000000**20#", "**67*+302100000000#", "**62*+302100000000#"]
    assert forwarding_codes("+302100000000", "full")[0]["code"] == "**21*+302100000000#"
