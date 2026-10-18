"""Capability roles hold the same privileges whenever they were provisioned.

0008, 0009 and 0010 rename the functions they replace aside for rollback, keeping their grants. Roles
provisioned before 0010 therefore kept EXECUTE on a superseded version: acquisition on
apply_assembly_command_m31, _m32 or _m33 (provisioned before 0008, at 0008 or at 0009), review and
release on apply_synthetic_approval_m33. Nothing calls them, and they lack every check added since,
so a role could bypass the current commands. Roles provisioned after 0015 got the guard SELECT on
policy.recovery_record through v0003's SELECT ON ALL TABLES; 0015 grants the guard its other three
tables only. All are revoked here from roles that exist. The provision bundles grant none of them,
and v0015 revokes the last again after v0003. tests/test_postgres_provision_order.py compares every
provisioning revision with provisioning after migrating.
"""

from alembic import op
from sqlalchemy import text

from rights.database import PolicyNamespace

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

REVOKES = (
    ("acquisition", "EXECUTE ON FUNCTION {p}.apply_assembly_command_m31(jsonb)"),
    ("acquisition", "EXECUTE ON FUNCTION {p}.apply_assembly_command_m32(jsonb)"),
    ("acquisition", "EXECUTE ON FUNCTION {p}.apply_assembly_command_m33(jsonb)"),
    ("review", "EXECUTE ON FUNCTION {p}.apply_synthetic_approval_m33(jsonb)"),
    ("release", "EXECUTE ON FUNCTION {p}.apply_synthetic_approval_m33(jsonb)"),
    ("guard", "SELECT ON {p}.recovery_record"),
)


def upgrade():
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    quote = conn.dialect.identifier_preparer.quote_identifier
    policy = quote(ns.schema("policy"))
    for capability, privilege in REVOKES:
        role = ns.role(capability)
        if conn.scalar(text("SELECT to_regrole(:role)"), {"role": role}) is not None:
            op.execute(f"REVOKE {privilege.format(p=policy)} FROM {quote(role)}")


def downgrade():
    # Nothing to restore: the current functions never needed these privileges, and roles
    # provisioned after migrating never held the superseded functions' grants.
    pass
