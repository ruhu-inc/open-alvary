"""The website's catalogue on PostgreSQL gives what the former in-Python scan gave: the same matches,
ranks, order, totals and filter counts, for titles with "nº", "ß", Greek and no year, and a source
whose cleared text matches (api.catalogue.browse, api.repositories.browse_rows)."""

from collections import Counter

import pytest
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.catalogue import FACETS, LegacyCatalogue
from api.repositories import LegacyRepository
from api.search import normalize_citation
from api.store import Store
from rights.policy import decide

pytestmark = pytest.mark.postgres

TITLES = [
    "Loi nº 12/2004 portant code de procédure",
    "ITEGEKO Nº 5/2010 RYO KU WA 01/02/2010",
    "Straße und Wege Gesetz",
    "Νόμος για τον Σίσυφο",
    "Law n° 43/2016 establishing the employment service",
    "  Spaced   out    Act  ",
    "Mining Act",
    "Regulations on mining and quarries",
]


def reference(service, *, query=None, filters=None, sort="relevance", limit=20, offset=0):
    """The website's catalogue as it was computed before (every source read in Python)."""
    filters = {k: v for k, v in (filters or {}).items() if v is not None}
    term = normalize_citation(query) if query is not None else None
    candidates = []
    for source, rights in service.repository.sources():
        if not decide(rights, now=service.now).metadata:
            continue
        rank = 0
        if term is not None:
            if term in normalize_citation(source.title):
                rank = 3
            elif term in normalize_citation(source.citation):
                rank = 2
            elif term in normalize_citation(source.summary or ""):
                rank = 1
            elif any(term in v.canonical_text.casefold() for v in service._approved(source, rights)):
                rank = 1
            else:
                continue
        has_text = next(service._approved(source, rights), None) is not None
        values = {
            "jurisdiction": source.jurisdiction_id,
            "type": source.document_type,
            "language": source.language,
            "status": source.legal_status,
            "year": str(source.year) if source.year else None,
            "access": "open" if has_text else "metadata",
        }
        candidates.append((rank, source, values))

    def selected(values, skip=None):
        return all(values[k] == v for k, v in filters.items() if k != skip)

    results = [c for c in candidates if selected(c[2])]
    facets = {
        name: Counter(c[2][name] for c in candidates if selected(c[2], skip=name) and c[2][name])
        for name in FACETS
    }
    by_title = lambda c: c[1].title.casefold()  # noqa: E731
    if sort == "relevance":
        results.sort(key=lambda c: (-c[0], by_title(c)))
    elif sort == "title":
        results.sort(key=by_title)
    else:
        dated = [c for c in results if c[1].year]
        results = sorted(dated, key=lambda c: (c[1].year, by_title(c)), reverse=sort == "newest") + sorted(
            (c for c in results if not c[1].year), key=by_title
        )
    return (
        len(results),
        [c[1].id for c in results[offset : offset + limit]],
        {name: dict(counts.most_common()) for name, counts in facets.items()},
    )


def test_the_database_catalogue_matches_the_former_scan(pg_engine, cleared):
    migrate(pg_engine)
    base, rights = cleared.sources[1], cleared.rights[1]
    for n, title in enumerate(TITLES * 4):
        source = base.model_copy(
            update={
                "id": f"fixture-{n:03d}",
                "title": title,
                "citation": f"Cap {n}" if n % 3 else "Straße 7",
                "summary": "About mining in the Northern Province." if n % 5 == 0 else None,
                "year": None if n % 7 == 0 else 1990 + n,
                "document_type": ("act", "regulation", "other")[n % 3],
                "legal_status": ("current", "repealed", "unknown")[n % 3],
            }
        )
        cleared.sources.append(source)
        cleared.rights.append(rights.model_copy(update={"id": f"rights-{source.id}", "source_id": source.id}))
    Store(pg_engine).save(cleared)
    queries = [None, "act", "nº 12", "no 12", "itegeko nº 5", "strasse", "STRASSE", "straße", "σίσυφο",
               "spaced out", "mining", "synthetic fixture", "test only", "nothing at all", "cap 1"]  # fmt: skip
    filter_sets = [{}, {"type": "act"}, {"status": "repealed", "type": "regulation"}, {"access": "open"},
                   {"year": "1995"}, {"jurisdiction": "ng", "language": "en"}]  # fmt: skip
    with pg_engine.connect() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        assert service.repository._unicode_lower()  # the database folds as Python does here
        for query in queries:
            for filters in filter_sets:
                for sort in ("relevance", "title", "newest", "oldest"):
                    for offset in (0, 20):
                        page, facets = service.browse(
                            query=query, filters=filters, sort=sort, limit=20, offset=offset
                        )
                        got = (page.total, [s.id for s in page.items], facets)
                        expected = reference(service, query=query, filters=filters, sort=sort, offset=offset)
                        assert got == expected, (query, filters, sort, offset)
        assert service.coverage().catalogued_sources == len(cleared.sources)
