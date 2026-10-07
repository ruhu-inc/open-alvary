"""Explicit maintenance role provisioning and bounded, transaction-owned policy reads.

No automatic provisioning/startup hook, login provider, or publication command exists.
The roles are NOLOGIN capability groups. M2 must provision real individual identities.
"""

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.engine import Connection

from schema.policy import MetadataPage, PolicyResult, PublicMetadata


@dataclass(frozen=True)
class PolicyNamespace:
    base: str = "public"

    def schema(self, kind: str) -> str:
        if kind not in {"policy", "staging", "corpus"}:
            raise ValueError("Unknown policy schema")
        return f"{self.base}_{kind}" if self.base.startswith("test_") else kind

    def role(self, kind: str) -> str:
        if kind not in {"guard", "serving", "acquisition", "review", "release"}:
            raise ValueError("Unknown database capability")
        return "oa_" + sha256(self.base.encode()).hexdigest()[:12] + "_" + kind

    def qualified(self, conn: Connection, kind: str, name: str) -> str:
        quote = conn.dialect.identifier_preparer.quote_identifier
        return f"{quote(self.schema(kind))}.{quote(name)}"


def provision_roles(conn: Connection, namespace: PolicyNamespace) -> dict[str, str]:
    """Explicit atomic administrator action; reject reuse and incomplete capabilities."""
    from rights.capabilities import selected_grants

    # Inventory and validation happen before creating any roles or grants.
    grants, functions = selected_grants(conn, namespace)
    quote = conn.dialect.identifier_preparer.quote_identifier
    roles = {kind: namespace.role(kind) for kind in ("guard", "serving", "acquisition", "review", "release")}
    existing = conn.scalar(
        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY(:names))"),
        {"names": list(roles.values())},
    )
    if existing:
        raise ValueError("Capability role already exists; inspect grants before reprovisioning")
    for name in roles.values():
        conn.execute(
            text(
                f"CREATE ROLE {quote(name)} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
            )
        )
    guard = quote(roles["guard"])
    administrator = quote(conn.scalar(text("SELECT current_user")))
    conn.execute(text(f"GRANT {guard} TO {administrator}"))
    for grant in grants:
        conn.exec_driver_sql(grant, execution_options={"no_parameters": True})
    policy, corpus = (quote(namespace.schema(k)) for k in ("policy", "corpus"))
    conn.execute(text(f"GRANT CREATE ON SCHEMA {policy},{corpus} TO {guard}"))
    # Signatures are safely quoted by PostgreSQL's catalog, never supplied by a client.
    for signature in functions:
        conn.execute(text(f"ALTER FUNCTION {signature} OWNER TO {guard}"))
    conn.execute(text(f"ALTER VIEW {corpus}.eligible_metadata OWNER TO {guard}"))
    conn.execute(text(f"REVOKE CREATE ON SCHEMA {policy},{corpus} FROM {guard}"))
    return roles


class PolicyRepository:
    """No implicit commits, raw evidence reads, or arbitrary operation SQL."""

    def __init__(self, connection: Connection, namespace: PolicyNamespace):
        self.connection = connection
        self.namespace = namespace

    def public_decision(self, collection_id: str, operation: str) -> PolicyResult:
        # Later text operations need M2/M3 atomic approval and M4 delivery authority.
        if operation != "redistribute_metadata":
            return PolicyResult(allowed=False, reason="runtime_capability_unavailable")
        function = self.namespace.qualified(self.connection, "policy", "metadata_allowed")
        allowed = bool(self.connection.scalar(text(f"SELECT {function}(:id)"), {"id": collection_id}))
        return PolicyResult(allowed=allowed, reason="permitted" if allowed else "policy_denied")

    def metadata(self, work_id: str) -> PublicMetadata | None:
        view = self.namespace.qualified(self.connection, "corpus", "eligible_metadata")
        row = (
            self.connection.execute(text(f"SELECT * FROM {view} WHERE work_id=:id"), {"id": work_id})
            .mappings()
            .one_or_none()
        )
        return PublicMetadata.model_validate(dict(row)) if row is not None else None

    def metadata_page(self, *, limit: int = 50, after: str | None = None) -> MetadataPage:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid page bounds")
        view = self.namespace.qualified(self.connection, "corpus", "eligible_metadata")
        where = "WHERE work_id>:after" if after is not None else ""
        rows = (
            self.connection.execute(
                text(f"SELECT * FROM {view} {where} ORDER BY work_id LIMIT :limit"),
                {"after": after, "limit": limit + 1},
            )
            .mappings()
            .all()
        )
        items = tuple(PublicMetadata.model_validate(dict(row)) for row in rows[:limit])
        return MetadataPage(items=items, next_after=items[-1].work_id if len(rows) > limit else None)

    def lock_collection(self, collection_id: str, *, expected_revision: int) -> int:
        """Maintenance/release transaction interface; never usable by a serving role."""
        function = self.namespace.qualified(self.connection, "policy", "lock_collection")
        return self.connection.scalar(
            text(f"SELECT {function}(:id,:revision)"), {"id": collection_id, "revision": expected_revision}
        )

    def lock_approval_inputs(
        self, collection_id: str, *, rights_revision: int, content_revision: int
    ) -> bool:
        """Lock/recheck independent review inputs, without approving or publishing content."""
        function = self.namespace.qualified(self.connection, "policy", "lock_approval_inputs")
        return bool(
            self.connection.scalar(
                text(f"SELECT {function}(:id,:rights,:content)"),
                {"id": collection_id, "rights": rights_revision, "content": content_revision},
            )
        )
