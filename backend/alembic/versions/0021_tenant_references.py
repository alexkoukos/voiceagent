"""Composite references prevent a valid foreign ID from pointing into another tenant."""
PARENTS = ("staff", "customers", "calls", "appointments", "config_versions")
REFERENCES = (
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

from alembic import op
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    for parent in PARENTS:
        op.create_unique_constraint(f"uq_{parent}_tenant_id", parent, ["practice_id", "id"])
    for child, column, parent in REFERENCES:
        op.create_foreign_key(f"fk_{child}_{column}_tenant", child, parent,
                              ["practice_id", column], ["practice_id", "id"])


def downgrade():
    for child, column, parent in reversed(REFERENCES):
        op.drop_constraint(f"fk_{child}_{column}_tenant", child, type_="foreignkey")
    for parent in reversed(PARENTS):
        op.drop_constraint(f"uq_{parent}_tenant_id", parent, type_="unique")
