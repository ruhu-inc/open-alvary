"""Seat limits per plan and Stripe billing (docs: teams-and-seats §5, stripe-billing §4).

accounts.organisation gains seat_grace_until, the moment an organisation over its plan's seats stops being
let off, and starter_country, the one country Starter covers once an owner has chosen it (until then the
plan's default, allowance.default_jurisdiction);
accounts.entitlement gains seats, an operator's seat count for a contract. Three tables mirror
Stripe: billing_customer (the organisation's customer and its open Checkout Session), billing_subscription
(each subscription as webhooks last told it) and stripe_webhook_event (each event once). Paid access itself
stays in accounts.entitlement, source 'payment' and reference the subscription.

policy.organisation_seat_limit(text) is the one rule for how many members access allows; it runs as its
caller, like accounts_access. policy.check_credential(jsonb) is replaced to refuse a member's own key or app
with seat_limit_exceeded once the grace has passed, counting members only then, and to give Starter's
scope as its chosen country (else the plan's default, else none) when Starter has a number of countries; signature, owner and
grants stay. No organisation's grace is set here: the limits come from scripts.seed afterwards, and the one-time
scripts.seat_grace gives each organisation then over its limit 30 days and tells its owners.

The logins reach the new tables and function once rights/logins.py names them (the migrate job's last step).
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace
from rights.sql_resources import install, install_new, remove_new

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

RESOURCE_HASH = "d99e7c65d55fe70e662821e822531b83cba2a4b3d3b9969727b0f91b265b1f43"


def namespace():
    return PolicyNamespace(op.get_bind().scalar(text("SELECT current_schema()")))


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    install_new(conn, ns, "v0059", RESOURCE_HASH, ())
    install(conn, ns, "v0059", RESOURCE_HASH)


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    conn, ns = op.get_bind(), namespace()
    for table in ("billing_customer", "billing_subscription"):
        if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {ns.qualified(conn, 'accounts', table)})")):
            raise RuntimeError("Billing records exist; a downgrade would delete them")
    entitlement = ns.qualified(conn, "accounts", "entitlement")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {entitlement} WHERE seats IS NOT NULL)")):
        raise RuntimeError("A grant holds a seat count; a downgrade would lose it")
    organisation = ns.qualified(conn, "accounts", "organisation")
    if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {organisation} WHERE starter_country IS NOT NULL)")):
        raise RuntimeError("An organisation has chosen Starter's country; a downgrade would lose it")
    install(conn, ns, "v0059", RESOURCE_HASH, downgrade=True)
    remove_new(conn, ns, "v0059", RESOURCE_HASH)
