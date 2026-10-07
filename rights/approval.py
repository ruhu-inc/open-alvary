"""Native-session private synthetic approval and explicit administrator identity binding."""

import json
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text

from rights.acquisition import AcquisitionError, SyntheticAcquisition
from rights.commands import MAX_COMMAND_BYTES
from rights.database import PolicyNamespace
from schema.approval import ApprovalCommand, ApprovalResult, PrivateVersionView
from schema.identity import ExpressionID, ManifestationID

ADAPTER = TypeAdapter(ApprovalCommand)


def parse_approval_command(raw: bytes) -> ApprovalCommand:
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("Bounded private command required")
    return ADAPTER.validate_json(raw)


def declare_synthetic_binding(
    connection,
    namespace: PolicyNamespace,
    *,
    binding_id: UUID,
    expression_id: ExpressionID,
    manifestation_id: ManifestationID,
    artifact_id: UUID,
    evidence_id: UUID,
    reason: str,
) -> str:
    """Administrator-only declaration referencing existing explicitly registered identities.

    Creates no work, expression, manifestation, observation or legal facts; caller owns transaction.
    Runtime groups have no INSERT on the declaration table or identity registry.
    """
    if not 1 <= len(reason.strip()) <= 2048:
        raise ValueError("Explicit synthetic binding reason required")
    with connection.begin_nested():
        quote = connection.dialect.identifier_preparer.quote_identifier
        base = quote(namespace.base)
        p, s = (quote(namespace.schema(kind)) for kind in ("policy", "staging"))
        # Native subjects must fail before identity/payload loading.
        table = f"{s}.synthetic_expression_binding"
        if not connection.scalar(
            text("SELECT has_table_privilege(session_user,:table,'INSERT')"), {"table": table}
        ):
            raise ValueError("Administrator declaration required")
        connection.execute(text(f"SELECT {base}.identity_check_legacy_authority(true)"))
        connection.execute(
            text(f"""
            INSERT INTO {table}(id,expression_id,manifestation_id,artifact_id,collection_id,work_id,
                artifact_hash,size_bytes,evidence_id,reason,identity_hash)
            SELECT :id,x.id,m.id,a.id,a.collection_id,w.id,a.artifact_hash,a.size_bytes,:evidence,:reason,
                encode(sha256(convert_to(jsonb_build_object('work',to_jsonb(w),'expression',to_jsonb(x),
                    'manifestation',to_jsonb(m))::text,'UTF8')),'hex')
            FROM {base}.identity_expressions x JOIN {base}.identity_works w ON w.id=x.work_id
            JOIN {base}.identity_work_manifestations wm ON wm.work_id=w.id
            JOIN {base}.identity_manifestations m ON m.id=wm.manifestation_id
            JOIN {s}.staged_artifact a ON a.artifact_hash=m.raw_hash AND a.size_bytes=m.size_bytes
                AND a.collection_id=w.collection_id
            WHERE x.id=:expression AND m.id=:manifestation AND a.id=:artifact
                AND w.identity_status='identified'
            RETURNING id
        """),
            dict(
                id=binding_id,
                expression=expression_id,
                manifestation=manifestation_id,
                artifact=artifact_id,
                evidence=evidence_id,
                reason=reason.strip(),
            ),
        ).scalar_one()
        fn = namespace.qualified(connection, "policy", "synthetic_binding_hash")
        return connection.scalar(text(f"SELECT {fn}(:id)"), {"id": binding_id})


class SyntheticApproval(SyntheticAcquisition):
    def apply(self, command: ApprovalCommand) -> ApprovalResult:
        raw = json.dumps(ADAPTER.validate_python(command).model_dump(mode="json"), ensure_ascii=False)
        if len(raw.encode()) > MAX_COMMAND_BYTES:
            return ApprovalResult(command_id=command.command_id, status="validation_failed")
        fn = self.namespace.qualified(self.connection, "policy", "apply_synthetic_approval")
        try:
            result = self._scalar(text(f"SELECT {fn}(CAST(:command AS jsonb))"), {"command": raw})
            return ApprovalResult.model_validate(result)
        except AcquisitionError as error:
            return ApprovalResult(command_id=command.command_id, status=error.status)

    def read(self, version_id: str, *, purpose: str) -> PrivateVersionView | None:
        fn = self.namespace.qualified(self.connection, "policy", "read_approved_synthetic_version")
        try:
            result = self._scalar(text(f"SELECT {fn}(:id,:purpose)"), {"id": version_id, "purpose": purpose})
        except AcquisitionError as error:
            if error.status == "forbidden":
                return None
            raise
        return PrivateVersionView.model_validate(result) if result is not None else None
