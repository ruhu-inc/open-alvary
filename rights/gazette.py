"""Caller-owned private synthetic gazette correspondence, without new byte owners."""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text

from rights.acquisition import AcquisitionError, SyntheticAcquisition
from rights.commands import MAX_COMMAND_BYTES
from schema.gazette import GazetteCommand, GazetteProjection, GazetteResult

ADAPTER = TypeAdapter(GazetteCommand)


def parse_gazette_command(raw: bytes) -> GazetteCommand:
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("Bounded private command required")
    return ADAPTER.validate_json(raw)


def declare_synthetic_item_binding(
    connection,
    namespace,
    *,
    binding_id: UUID,
    item_expression_id: str,
    issue_version_id: str,
    notice_node_id: str,
    evidence_id: UUID,
    reason: str,
) -> str:
    """Administrator-only reference to existing identified item/issue identities; no minting."""
    if not 1 <= len(reason.strip()) <= 2048:
        raise ValueError("Explicit synthetic identification reason required")
    with connection.begin_nested():
        table = namespace.qualified(connection, "staging", "gazette_item_binding")
        if not connection.scalar(
            text("SELECT has_table_privilege(session_user,:table,'INSERT')"), {"table": table}
        ):
            raise ValueError("Administrator declaration required")
        connection.execute(
            text(
                f"INSERT INTO {table}(id,item_expression_id,issue_version_id,notice_node_id,evidence_id,reason) VALUES(:id,:expression,:version,:node,:evidence,:reason)"
            ),
            dict(
                id=binding_id,
                expression=item_expression_id,
                version=issue_version_id,
                node=notice_node_id,
                evidence=evidence_id,
                reason=reason.strip(),
            ),
        )
        fn = namespace.qualified(connection, "policy", "gazette_binding_hash")
        return connection.scalar(text(f"SELECT {fn}(:id)"), {"id": binding_id})


class SyntheticGazette(SyntheticAcquisition):
    def apply(self, command: GazetteCommand) -> GazetteResult:
        raw = ADAPTER.validate_python(command).model_dump_json()
        if len(raw.encode()) > MAX_COMMAND_BYTES:
            return GazetteResult(command_id=command.command_id, status="validation_failed")
        fn = self.namespace.qualified(self.connection, "policy", "apply_gazette_command")
        try:
            result = self._scalar(text(f"SELECT {fn}(CAST(:command AS jsonb))"), {"command": raw})
            return GazetteResult.model_validate(result)
        except AcquisitionError as error:
            return GazetteResult(command_id=command.command_id, status=error.status)

    def read(self, correspondence_id: UUID, *, purpose: str) -> GazetteProjection | None:
        fn = self.namespace.qualified(self.connection, "policy", "read_gazette_projection")
        try:
            result = self._scalar(
                text(f"SELECT {fn}(:id,:purpose)"), {"id": correspondence_id, "purpose": purpose}
            )
        except AcquisitionError as error:
            if error.status == "forbidden":
                return None
            raise
        return GazetteProjection.model_validate(result) if result is not None else None
