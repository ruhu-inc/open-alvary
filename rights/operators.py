"""Explicit maintenance binding to existing PostgreSQL logins; no password service.

Only synthetic individual appointments are supported until production decisions exist.
Neither this adapter nor the CLI creates login credentials or commits for callers.
"""

from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from rights.database import PolicyNamespace
from schema.identity import CollectionID
from schema.models import Hash


class SyntheticAppointment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    subject_issuer: str = Field(min_length=1, max_length=160)
    subject_key: str = Field(min_length=1, max_length=256)
    authenticated_until: AwareDatetime
    evidence_reference: str = Field(min_length=1, max_length=2048)
    evidence_hash: Hash
    reason: str = Field(min_length=1, max_length=2048)


def register_operator(
    connection: Connection,
    namespace: PolicyNamespace,
    *,
    login_name: str,
    appointment: SyntheticAppointment,
    assignments: tuple[tuple[CollectionID, str], ...],
) -> UUID:
    """Administrator-only transaction; bind one private subject to one native login.

    Evidence/issuer/subject are trusted maintenance inputs, not CLI authority claims.
    Login authentication must already be configured using PostgreSQL or a selected provider.
    """
    if connection.dialect.name != "postgresql":
        raise ValueError("PostgreSQL required")
    if not assignments or any(
        cap not in {"rights", "content", "acquire", "release"} for _, cap in assignments
    ):
        raise ValueError("Explicit collection capabilities required")
    with connection.begin_nested():
        subject = (
            connection.execute(
                text("""
            SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,
                (rolvaliduntil IS NULL OR rolvaliduntil>statement_timestamp()) AS credential_current
            FROM pg_roles WHERE rolname=:name
            """),
                {"name": login_name},
            )
            .mappings()
            .one_or_none()
        )
        if (
            subject is None
            or not subject["rolcanlogin"]
            or not subject["credential_current"]
            or any(
                subject[flag]
                for flag in ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication")
            )
        ):
            raise ValueError("Dedicated nonprivileged login required")
        safe = namespace.qualified(connection, "policy", "operator_role_safe")
        if not connection.scalar(text(f"SELECT {safe}(:role)"), {"role": subject["oid"]}):
            raise ValueError("Unexpected inherited authority")
        now = connection.scalar(text("SELECT statement_timestamp()"))
        if appointment.authenticated_until <= now:
            raise ValueError("Current appointment required")
        actor_id = uuid4()
        actor = namespace.qualified(connection, "policy", "actor")
        connection.execute(
            text(f"""
            INSERT INTO {actor}(id,database_role,identity_kind,active,database_role_name,
                subject_issuer,subject_key,authenticated_until,binding_evidence_reference,
                binding_evidence_hash,registration_reason)
            VALUES(:id,:role,'synthetic',true,:name,:issuer,:subject,:until,:reference,:hash,:reason)
            """),
            {
                "id": actor_id,
                "role": subject["oid"],
                "name": login_name,
                "issuer": appointment.subject_issuer,
                "subject": appointment.subject_key,
                "until": appointment.authenticated_until,
                "reference": appointment.evidence_reference,
                "hash": appointment.evidence_hash,
                "reason": appointment.reason,
            },
        )
        table = namespace.qualified(connection, "policy", "assignment")
        for collection, capability in set(assignments):
            connection.execute(
                text(
                    f"INSERT INTO {table}(actor_id,collection_id,capability) VALUES(:actor,:collection,:cap)"
                ),
                {"actor": actor_id, "collection": collection, "cap": capability},
            )
        kinds = {
            "review" if cap in {"rights", "content"} else "acquisition" if cap == "acquire" else "release"
            for _, cap in assignments
        }
        quote = connection.dialect.identifier_preparer.quote_identifier
        for kind in sorted(kinds):
            connection.execute(text(f"GRANT {quote(namespace.role(kind))} TO {quote(login_name)}"))
        return actor_id


def revoke_operator(
    connection: Connection, namespace: PolicyNamespace, actor_id: UUID, *, reason: str
) -> None:
    """Administrator revocation: locks out new commands and current policy actor checks."""
    reason = reason.strip()
    if not 1 <= len(reason) <= 2048:
        raise ValueError("Bounded reason required")
    actor = namespace.qualified(connection, "policy", "actor")
    result = connection.execute(
        text(
            f"UPDATE {actor} SET active=false,registration_reason=:reason WHERE id=:id AND database_role_name IS NOT NULL"
        ),
        {"id": actor_id, "reason": reason},
    )
    if result.rowcount != 1:
        raise ValueError("Registered operator required")
