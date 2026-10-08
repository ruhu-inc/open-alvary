"""BUD byte reservations over the absolute-byte ledger (migration 0017).

Each step is its own short transaction; no transaction is held while bytes are transmitted.
Order for a buffered response: reserve (charged at once) → recheck policy → begin_emission
(durable before any byte reaches transport) → write → settle with the transport receipt.
Only provably unsent reservations are released; ambiguous delivery stays charged until the
maintenance reconciler settles it conservatively.
"""

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from rights.database import PolicyNamespace


class LedgerUnavailable(RuntimeError):
    pass


class LedgerConflict(RuntimeError):
    """A late, stale or conflicting transition; the reservation stays as it was."""


@dataclass(frozen=True)
class Scope:
    key: str
    capacity: int
    refill_per_minute: float


@dataclass(frozen=True)
class Reservation:
    reserved: bool
    reservation_id: UUID | None = None
    blocked: tuple[str, ...] = ()
    retry_after: int | None = None


class ByteLedger:
    def __init__(self, engine: Engine, namespace: PolicyNamespace):
        self.engine = engine
        self.namespace = namespace

    def _call(self, name, args, parameters):
        try:
            with self.engine.begin() as conn:
                fn = self.namespace.qualified(conn, "policy", name)
                return conn.scalar(text(f"SELECT {fn}({args})"), parameters)
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "40001":
                raise LedgerConflict("Byte reservation transition refused") from None
            raise LedgerUnavailable("Byte ledger unavailable") from None
        except Exception:
            raise LedgerUnavailable("Byte ledger unavailable") from None

    def reserve(
        self, admission_id: UUID, nbytes: int, scopes: list[Scope], lease_seconds: float
    ) -> Reservation:
        request = {
            "admission_id": str(admission_id),
            "bytes": nbytes,
            "lease_seconds": lease_seconds,
            "scopes": [
                {"key": s.key, "capacity": s.capacity, "refill_per_minute": s.refill_per_minute}
                for s in scopes
            ],
        }
        result = self._call("reserve_bytes", "CAST(:r AS jsonb)", {"r": json.dumps(request)})
        if result["reserved"]:
            return Reservation(True, UUID(result["reservation_id"]))
        return Reservation(False, blocked=tuple(result["blocked"]), retry_after=result["retry_after"])

    def begin_emission(self, reservation_id: UUID) -> bool:
        return self._call("begin_emission", ":r", {"r": reservation_id})

    def release(self, reservation_id: UUID) -> None:
        self._call("release_bytes", ":r", {"r": reservation_id})

    def settle(self, reservation_id: UUID, sent: int, receipt: str) -> None:
        self._call("settle_bytes", ":r,:s,:t", {"r": reservation_id, "s": sent, "t": receipt})

    def reconcile(self, fence_seconds: float, conservative_seconds: float) -> dict:
        """Maintenance only: D3 fixes both bounds; there are no defaults."""
        return self._call("reconcile_bytes", ":f,:c", {"f": fence_seconds, "c": conservative_seconds})

    def reconcile_reservation(self, reservation_id: UUID, measured: int, receipt: str) -> None:
        self._call("reconcile_reservation", ":r,:m,:t", {"r": reservation_id, "m": measured, "t": receipt})
