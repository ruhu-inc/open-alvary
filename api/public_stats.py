"""The home page's live totals (/site/v1/stats, owner 2026-11-23).

What is in the catalogue (published texts, public records by country and kind, the change log's last 30 days)
and how the site and the API are used over the last 30 days. Every number is worked out from what is already
kept: the catalogue and the public change log, read as any visitor may read them, and the day totals the
website and the credential gate count, read through policy.public_usage_totals() (migration 0058), which
returns totals over everyone and withholds any under 10. Nothing here is about one visitor, organisation,
credential or search.

Near real time: the totals are worked out at most once every CACHE_SECONDS per instance and shared by every
request in between, so a busy home page costs the database one query set a minute at most. The traffic counts
themselves reach the database within about a minute of the visit (api.traffic.FLUSH_SECONDS).
"""

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import Engine, text

from api.catalogue import FACETS, LegacyCatalogue
from changes.feed import Feed
from rights.database import PolicyNamespace

CACHE_SECONDS = 30.0
WINDOW_DAYS = 30


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class PublicStats:
    def __init__(
        self,
        engine: Engine,
        catalogue: Callable[[], AbstractContextManager[LegacyCatalogue]],
        changes: Callable[[], AbstractContextManager] | None,
        namespace: PolicyNamespace,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.engine, self.catalogue, self.changes, self.clock = engine, catalogue, changes, clock
        self.namespace = namespace
        self._lock = threading.Lock()
        self._held: tuple[float, dict] | None = None

    def current(self) -> dict:
        """The totals, worked out again when the held ones are older than CACHE_SECONDS."""
        with self._lock:
            if self._held is not None and self.clock() - self._held[0] < CACHE_SECONDS:
                return self._held[1]
            fresh = self._work_out()
            self._held = (self.clock(), fresh)
            return fresh

    def _work_out(self) -> dict:
        with self.catalogue() as service:
            coverage = service.coverage()
            _, facets = service.browse(
                query=None, filters={name: None for name in FACETS}, sort="title", limit=1, offset=0
            )
            counts = {scope.jurisdiction: scope.count for scope in coverage.jurisdictions}
            ids = sorted(counts)
            rows = service.repository.rows("jurisdictions", ids=ids) if ids else []
            names = {row["id"]: row.get("name") for row in rows}
        kinds = sorted(facets.get("type", {}).items(), key=lambda item: (-item[1], item[0]))
        published = coverage.open_full_text_sources
        catalogue = {
            "verified_texts": published,
            "official_sources": coverage.catalogued_sources,
            "jurisdictions": [{"id": j, "name": names.get(j) or j.upper(), "count": counts[j]} for j in ids],
            "kinds": [{"value": value, "count": count} for value, count in kinds],
            # A text is published only once its rights are cleared, so every published text has a recorded
            # basis; with none published there is nothing to give a share of.
            "rights_basis_share": 1.0 if published else None,
            "changes": None,
            "last_change_at": None,
            "changes_complete": True,
        }
        if self.changes is not None:
            with self.changes() as connection:
                shown, newest, whole = Feed(connection, None).shown_since(WINDOW_DAYS)
            catalogue.update(changes=shown, last_change_at=_iso(newest), changes_complete=whole)
        return {
            "as_of": _iso(datetime.now(UTC)),
            "window_days": WINDOW_DAYS,
            "catalogue": catalogue,
            "usage": self._usage(),
            "notice": "Totals only. A usage total under 10 is given as null. Searched words are never stored.",
        }

    def _usage(self) -> dict | None:
        """The function's totals, or None where it is not installed (SQLite, or before migration 0058)."""
        if self.engine.dialect.name != "postgresql":
            return None
        with self.engine.begin() as connection:
            function = self.namespace.qualified(connection, "policy", "public_usage_totals")
            if connection.scalar(text("SELECT to_regprocedure(:f)"), {"f": f"{function}()"}) is None:
                return None
            totals = connection.scalar(text(f"SELECT {function}()"))
        return dict(totals) if totals is not None else None
