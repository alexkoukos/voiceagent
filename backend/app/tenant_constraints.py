"""Composite references prevent a valid foreign ID from pointing into another tenant."""
PARENTS = ("staff", "customers", "calls", "appointments", "config_versions")
REFERENCES = (
    ("calendar_feeds", "staff_id", "staff"),
    ("imports", "version_id", "config_versions"),
    ("appointments", "call_id", "calls"),
    ("appointments", "staff_id", "staff"),
    ("appointments", "customer_id", "customers"),
    ("calls", "customer_id", "customers"),
    ("calls", "appointment_id", "appointments"),
    ("admin_links", "staff_id", "staff"),
    ("admin_requests", "staff_id", "staff"),
    ("calendar_connections", "staff_id", "staff"),
    ("messages", "call_id", "calls"),
    ("messages", "staff_id", "staff"),
    ("routing_events", "call_id", "calls"),
    ("handoffs", "call_id", "calls"),
    ("handoffs", "staff_id", "staff"),
    ("devices", "staff_id", "staff"),
)
