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
    """Run explicitly as a migration administrator, before any application adoption.

    Caller owns the transaction. Exact deterministic names avoid unrelated role reuse;
    provisioning refuses existing roles. Failed provisioning rolls back every grant.
    This changes ownership only of narrow functions/view, never private tables.
    """
    quote = conn.dialect.identifier_preparer.quote_identifier
    roles = {kind: namespace.role(kind) for kind in ("guard", "serving", "acquisition", "review", "release")}
    for name in roles.values():
        if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:name)"), {"name": name}):
            raise ValueError("Capability role already exists; inspect grants before reprovisioning")
        conn.execute(
            text(
                f"CREATE ROLE {quote(name)} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
            )
        )
    p, s, c = (quote(namespace.schema(kind)) for kind in ("policy", "staging", "corpus"))
    guard = quote(roles["guard"])
    administrator = quote(conn.scalar(text("SELECT current_user")))
    conn.execute(text(f"GRANT {guard} TO {administrator}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA {quote(namespace.base)},{p},{s},{c} TO {guard}"))
    conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {p},{s},{c} TO {guard}"))
    conn.execute(text(f"GRANT SELECT ON {quote(namespace.base)}.identity_collections TO {guard}"))
    conn.execute(text(f"GRANT INSERT,UPDATE ON {p}.revision_head TO {guard}"))
    conn.execute(text(f"GRANT INSERT ON {p}.audit_event TO {guard}"))
    has_commands = conn.scalar(
        text("SELECT to_regclass(:name) IS NOT NULL"),
        {"name": namespace.schema("policy") + ".command_receipt"},
    )
    if has_commands:
        conn.execute(text(f"GRANT INSERT ON {p}.command_receipt,{p}.operator_event,{p}.evidence TO {guard}"))
        conn.execute(text(f"GRANT UPDATE(active) ON {p}.actor TO {guard}"))
        conn.execute(
            text(
                f"GRANT INSERT ON {p}.collection_decision,{p}.verification_policy,{p}.verification_material TO {guard}"
            )
        )
    has_acquisition = conn.scalar(
        text("SELECT to_regclass(:name) IS NOT NULL"),
        {"name": namespace.schema("policy") + ".synthetic_staging_limit"},
    )
    if has_acquisition:
        conn.execute(
            text(
                f"GRANT INSERT ON {p}.controller_record,{p}.privacy_review,{p}.acquisition_assessment,{s}.staged_artifact TO {guard}"
            )
        )
        conn.execute(text(f"GRANT UPDATE(maximum_bytes) ON {p}.synthetic_staging_limit TO {guard}"))
    conn.execute(text(f"GRANT UPDATE ON {s}.staged_artifact TO {guard}"))
    conn.execute(text(f"GRANT CREATE ON SCHEMA {p},{c} TO {guard}"))
    functions = (
        conn.execute(
            text("""
        SELECT p.oid::regprocedure::text AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname=:schema
    """),
            {"schema": namespace.schema("policy")},
        )
        .scalars()
        .all()
    )
    # PostgreSQL regprocedure produces safely quoted identifiers/signatures from its catalog.
    for signature in functions:
        conn.execute(text(f"ALTER FUNCTION {signature} OWNER TO {guard}"))
    conn.execute(text(f"ALTER VIEW {c}.eligible_metadata OWNER TO {guard}"))
    conn.execute(text(f"REVOKE CREATE ON SCHEMA {p},{c} FROM {guard}"))
    serving, acquisition, review, release = (
        quote(roles[k]) for k in ("serving", "acquisition", "review", "release")
    )
    conn.execute(text(f"GRANT USAGE ON SCHEMA {p},{c} TO {serving},{release}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA {p},{s} TO {acquisition}"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA {p} TO {review}"))
    conn.execute(text(f"GRANT SELECT ON {c}.eligible_metadata TO {serving},{release}"))
    conn.execute(text(f"GRANT SELECT,INSERT,UPDATE ON {c}.metadata_projection TO {release}"))
    conn.execute(text(f"GRANT SELECT,INSERT,UPDATE ON {s}.staged_artifact TO {acquisition}"))
    reviewed = (
        "controller_record",
        "privacy_review",
        "acquisition_assessment",
        "collection_decision",
        "verification_policy",
        "version_override",
    )
    for table in (*reviewed, "verification_material", "evidence", "version_binding"):
        privileges = "SELECT" if table in {"evidence", "version_binding"} else "SELECT,INSERT"
        conn.execute(text(f"GRANT {privileges} ON {p}.{quote(table)} TO {review}"))
    conn.execute(text(f"GRANT SELECT ON {p}.audit_event TO {review}"))
    for signature, grantees in (
        ("current_actor()", f"{review},{acquisition},{release}"),
        ("assigned(text,text)", f"{serving},{review},{acquisition},{release}"),
        ("metadata_allowed(text)", f"{serving},{release}"),
        ("staging_allowed(uuid,bigint,text,text)", acquisition),
        ("review_permission(text,text,text)", review),
        ("lock_collection(text,bigint)", f"{review},{release}"),
        ("lock_approval_inputs(text,bigint,bigint)", release),
        ("erase_staged(uuid)", acquisition),
    ):
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.{signature} TO {grantees}"))
    if has_commands:
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.apply_operator_command(jsonb) TO {review}"))
    if has_acquisition:
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.apply_acquisition_command(jsonb) TO {review}"))
        for signature in (
            "private_operation_allowed(uuid,bigint,text,text,text,text)",
            "is_native_operator()",
            "stage_synthetic_artifact(uuid,text,uuid,bigint,text,bytea,timestamptz)",
            "read_synthetic_artifact(uuid,text,text)",
        ):
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.{signature} TO {acquisition}"))
    if conn.scalar(
        text("SELECT to_regprocedure(:name) IS NOT NULL"),
        {"name": namespace.schema("policy") + ".reconcile_staging(jsonb)"},
    ):
        conn.execute(text(f"GRANT INSERT ON {p}.staging_lifecycle_event,{p}.lifecycle_receipt TO {guard}"))
        conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.reconcile_staging(jsonb) TO {acquisition}"))
    if conn.scalar(
        text("SELECT to_regclass(:name) IS NOT NULL"),
        {"name": namespace.schema("policy") + ".assembly_receipt"},
    ):
        for table in (
            "synthetic_run",
            "adapter_candidate",
            "staging_snapshot",
            "snapshot_head",
            "snapshot_selection",
            "snapshot_resolution",
        ):
            conn.execute(text(f"GRANT INSERT ON {s}.{table} TO {guard}"))
        conn.execute(text(f"GRANT INSERT ON {p}.assembly_receipt,{p}.assembly_lifecycle_event TO {guard}"))
        conn.execute(
            text(f"GRANT UPDATE(state,payload) ON {s}.adapter_candidate,{s}.staging_snapshot TO {guard}")
        )
        conn.execute(text(f"GRANT UPDATE(revision) ON {s}.snapshot_head TO {guard}"))
        for fn in (
            "apply_assembly_command(jsonb)",
            "read_candidates(uuid,text,uuid,integer)",
            "read_snapshot(uuid,bigint,text)",
        ):
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.{fn} TO {acquisition}"))
    if conn.scalar(
        text("SELECT to_regclass(:name) IS NOT NULL"),
        {"name": namespace.schema("staging") + ".canonical_proposal"},
    ):
        conn.execute(
            text(f"GRANT INSERT,UPDATE(state,payload,canonical_bytes) ON {s}.canonical_proposal TO {guard}")
        )
        for fn in (
            "canonical_inputs(jsonb)",
            "store_canonical_proposal(jsonb,bytea,jsonb)",
            "read_canonical_proposal(uuid,text)",
        ):
            conn.execute(text(f"GRANT EXECUTE ON FUNCTION {p}.{fn} TO {acquisition}"))
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
