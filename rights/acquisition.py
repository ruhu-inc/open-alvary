"""Caller-owned original-synthetic staging; no fetcher, observation writer or parser."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from rights.database import PolicyNamespace

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_SYNTHETIC_COLLECTION_BYTES = 100 * 1024 * 1024


class AcquisitionError(RuntimeError):
    """Safe failure for callers; database parameters/private bytes are never rendered."""

    def __init__(self, status: str):
        self.status = status
        super().__init__("Private acquisition request " + status)


def declare_synthetic_staging_limit(
    conn: Connection,
    namespace: PolicyNamespace,
    *,
    collection_id: str,
    maximum_bytes: int,
    evidence_id: UUID,
    reason: str,
) -> None:
    """Explicit immutable maintenance declaration; not an approved production profile."""
    if not 1 <= maximum_bytes <= MAX_SYNTHETIC_COLLECTION_BYTES or not 1 <= len(reason.strip()) <= 2048:
        raise ValueError("Bounded synthetic declaration required")
    table = namespace.qualified(conn, "policy", "synthetic_staging_limit")
    conn.execute(
        text(
            f"INSERT INTO {table}(collection_id,maximum_bytes,evidence_id,reason) VALUES(:c,:maximum,:evidence,:reason)"
        ),
        {"c": collection_id, "maximum": maximum_bytes, "evidence": evidence_id, "reason": reason.strip()},
    )


class SyntheticAcquisition:
    def __init__(self, connection: Connection, namespace: PolicyNamespace):
        self.connection = connection
        self.namespace = namespace

    def _scalar(self, statement, parameters):
        try:
            with self.connection.begin_nested():
                return self.connection.scalar(statement, parameters)
        except DBAPIError as error:
            code = getattr(error.orig, "sqlstate", None)
            status = (
                "forbidden"
                if code == "42501"
                else "conflict"
                if code in {"40001", "40P01", "23505"}
                else "capacity_exceeded"
                if code == "54000"
                else "validation_failed"
                if code and code[:2] in {"22", "23"}
                else "unavailable"
            )
            raise AcquisitionError(status) from None

    def allowed(
        self,
        *,
        collection_id: str,
        assessment_id: UUID,
        assessment_revision: int,
        purpose: str,
        artifact_hash: str,
        operation: str,
    ) -> bool:
        fn = self.namespace.qualified(self.connection, "policy", "private_operation_allowed")
        return bool(
            self._scalar(
                text(f"SELECT {fn}(:id,:revision,:collection,:purpose,:hash,:operation)"),
                {
                    "id": assessment_id,
                    "revision": assessment_revision,
                    "collection": collection_id,
                    "purpose": purpose,
                    "hash": artifact_hash,
                    "operation": operation,
                },
            )
        )

    def stage(
        self,
        *,
        artifact_id: UUID,
        collection_id: str,
        assessment_id: UUID,
        assessment_revision: int,
        purpose: str,
        private_bytes: bytes,
        retention_deadline: datetime,
    ) -> bool:
        """Return replay flag; never restore expired/erased bytes or retarget an artifact."""
        if len(private_bytes) > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact bound exceeded")
        if retention_deadline.tzinfo is None or retention_deadline.utcoffset() is None:
            raise ValueError("Aware retention deadline required")
        fn = self.namespace.qualified(self.connection, "policy", "stage_synthetic_artifact")
        return bool(
            self._scalar(
                text(f"SELECT {fn}(:artifact,:collection,:assessment,:revision,:purpose,:bytes,:deadline)"),
                {
                    "artifact": artifact_id,
                    "collection": collection_id,
                    "assessment": assessment_id,
                    "revision": assessment_revision,
                    "purpose": purpose,
                    "bytes": private_bytes,
                    "deadline": retention_deadline,
                },
            )
        )

    def read(self, artifact_id: UUID, *, purpose: str, operation: str = "retain") -> bytes | None:
        fn = self.namespace.qualified(self.connection, "policy", "read_synthetic_artifact")
        result = self._scalar(
            text(f"SELECT {fn}(:artifact,:purpose,:operation)"),
            {"artifact": artifact_id, "purpose": purpose, "operation": operation},
        )
        return bytes(result) if result is not None else None

    def erase(self, artifact_id: UUID) -> bool:
        fn = self.namespace.qualified(self.connection, "policy", "erase_staged")
        return bool(self._scalar(text(f"SELECT {fn}(:artifact)"), {"artifact": artifact_id}))
