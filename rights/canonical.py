"""Private cross-checked projection with caller-owned transaction and current lineage."""

from uuid import UUID

from sqlalchemy import text

from ingestion.canonical import CanonicalValidationError, compile_projection
from rights.acquisition import AcquisitionError, SyntheticAcquisition
from rights.commands import MAX_COMMAND_BYTES
from schema.assembly import AssemblyResult, CandidateInput
from schema.canonical import CanonicalCommand, CanonicalView


class CanonicalAssembler(SyntheticAcquisition):
    def apply(self, command: CanonicalCommand) -> AssemblyResult:
        raw = command.model_dump_json()
        if len(raw.encode()) > MAX_COMMAND_BYTES:
            return AssemblyResult(command_id=command.command_id, status="validation_failed")
        try:
            with self.connection.begin_nested():
                inputs_fn = self.namespace.qualified(self.connection, "policy", "canonical_inputs")
                inputs = self._scalar(text(f"SELECT {inputs_fn}(CAST(:command AS jsonb))"), {"command": raw})
                projection = compile_projection(
                    command.plan,
                    tuple(CandidateInput.model_validate(c) for c in inputs["candidates"]),
                    tuple(UUID(id) for id in inputs["order"]),
                )
                fn = self.namespace.qualified(self.connection, "policy", "store_canonical_proposal")
                result = self._scalar(
                    text(f"SELECT {fn}(CAST(:command AS jsonb),:bytes,CAST(:projection AS jsonb))"),
                    {
                        "command": raw,
                        "bytes": projection.canonical_bytes,
                        "projection": projection.details.model_dump_json(),
                    },
                )
                return AssemblyResult.model_validate(result)
        except CanonicalValidationError:
            return AssemblyResult(command_id=command.command_id, status="validation_failed")
        except AcquisitionError as error:
            return AssemblyResult(command_id=command.command_id, status=error.status)

    def read(self, proposal_id: UUID, *, purpose: str) -> CanonicalView | None:
        fn = self.namespace.qualified(self.connection, "policy", "read_canonical_proposal")
        try:
            result = self._scalar(text(f"SELECT {fn}(:id,:purpose)"), {"id": proposal_id, "purpose": purpose})
        except AcquisitionError as error:
            if error.status == "forbidden":
                return None
            raise
        return CanonicalView.model_validate(result) if result is not None else None
