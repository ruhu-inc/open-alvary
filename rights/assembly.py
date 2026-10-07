"""Caller-owned private synthetic proposals; no extraction, canonicalization or publication."""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text

from rights.acquisition import AcquisitionError, SyntheticAcquisition
from rights.commands import MAX_COMMAND_BYTES
from schema.assembly import AssemblyCommand, AssemblyResult, CandidatePage, SnapshotView


def parse_assembly_command(raw: bytes) -> AssemblyCommand:
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("Command too large")
    return TypeAdapter(AssemblyCommand).validate_json(raw)


class SyntheticAssembly(SyntheticAcquisition):
    def apply(self, command: AssemblyCommand) -> AssemblyResult:
        raw = command.model_dump_json()
        if len(raw.encode()) > MAX_COMMAND_BYTES:
            return AssemblyResult(command_id=command.command_id, status="validation_failed")
        fn = self.namespace.qualified(self.connection, "policy", "apply_assembly_command")
        try:
            result = self._scalar(text(f"SELECT {fn}(CAST(:raw AS jsonb))"), {"raw": raw})
        except AcquisitionError as error:
            return AssemblyResult(command_id=command.command_id, status=error.status)
        return AssemblyResult.model_validate(result)

    def candidates(
        self, run_id: UUID, *, purpose: str, after: UUID | None = None, limit: int = 50
    ) -> CandidatePage:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Bounded candidate page required")
        fn = self.namespace.qualified(self.connection, "policy", "read_candidates")
        try:
            result = self._scalar(
                text(f"SELECT {fn}(:run,:purpose,:after,:limit)"),
                {"run": run_id, "purpose": purpose, "after": after, "limit": limit},
            )
        except AcquisitionError as error:
            if error.status == "forbidden":
                return CandidatePage()
            raise
        return CandidatePage.model_validate(result)

    def snapshot(self, snapshot_id: UUID, revision: int, *, purpose: str) -> SnapshotView | None:
        if type(revision) is not int or not 1 <= revision <= 32:
            raise ValueError("Bounded snapshot revision required")
        fn = self.namespace.qualified(self.connection, "policy", "read_snapshot")
        try:
            result = self._scalar(
                text(f"SELECT {fn}(:id,:revision,:purpose)"),
                {"id": snapshot_id, "revision": revision, "purpose": purpose},
            )
        except AcquisitionError as error:
            if error.status == "forbidden":
                return None
            raise
        return SnapshotView.model_validate(result) if result is not None else None
