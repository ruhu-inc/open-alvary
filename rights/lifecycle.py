"""Bounded conservative reconciliation; caller owns commit and collection traversal."""

from sqlalchemy import text

from rights.acquisition import AcquisitionError, SyntheticAcquisition
from rights.commands import MAX_COMMAND_BYTES
from schema.operator import LifecycleRequest, LifecycleResult


def parse_lifecycle_request(raw: bytes) -> LifecycleRequest:
    if len(raw) > MAX_COMMAND_BYTES:
        raise ValueError("Command too large")
    return LifecycleRequest.model_validate_json(raw)


class AcquisitionLifecycle(SyntheticAcquisition):
    def reconcile(self, request: LifecycleRequest) -> LifecycleResult:
        fn = self.namespace.qualified(self.connection, "policy", "reconcile_staging")
        try:
            result = self._scalar(
                text(f"SELECT {fn}(CAST(:request AS jsonb))"),
                {"request": request.model_dump_json()},
            )
        except AcquisitionError as error:
            status = (
                error.status
                if error.status in {"forbidden", "conflict", "validation_failed"}
                else "unavailable"
            )
            return LifecycleResult(command_id=request.command_id, status=status)
        return LifecycleResult.model_validate(result)
