"""The website's catalogue from the columns of migrations 0045 and 0046 (api.catalogue.browse, and the
access filter of /sources and /search) gives what reading each record through the rights policy gives:
the same pages in the same order, the same totals and filter counts, whatever a record's rights or versions
say, at any time of asking. Nothing is read per record but the page's, and a withdrawal or an expiry takes
effect on the next answer (F01)."""

import random
import sys
from datetime import timedelta
from hashlib import sha256

import pytest
from alembic import command
from alembic.config import Config
from conftest import NOW
from sqlalchemy import event, text
from test_postgres import pg_engine as pg_engine
from test_postgres_coverage import (
    ASKED,
    EARLIER,
    LATER,
    RIGHTS_CHANGES,
    VERSION_CHANGES,
    add_record,
)

import api.catalogue
from api import text_index
from api.catalogue import FACETS, LegacyCatalogue
from api.repositories import LegacyRepository, ResultTooLarge
from api.search import has_every_word
from api.store import Store
from rights.policy import decide
from schema.models import Language, StructureNode

pytestmark = pytest.mark.postgres

# Titles whose order depends on exactly how case folds: what a sort by lower case or by a locale gets wrong.
TITLES = [
    "Straße Act", "STRASSE Act", "strasse act", "Strassen Act", "ſtrasse Act", "ß", "SS", "ss", "ẞ",
    "Σίσυφος", "σίσυφοσ", "ΣΊΣΥΦΟΣ", "Ꭰ Cherokee", "ꭰ cherokee", "ﬁnance Act", "Finance Act", "FINANCE ACT",
    "İzmir Act", "izmir Act", "ı Act", "Éducation Act", "éducation Act", "Education Act", "Zebra Act", "zebra act",
    "ÿ Act", "Ÿ Act", "ǅ Act", "ǆ Act", "Ǆ Act", "Loi nº 12/2004", "Loi n° 12/2004", "  Spaced   out    Act  ",
    "1 Act", "10 Act", "2 Act", "_ Act", "- Act", "Ab Act", "aB Act", "ab act",
    "x" * 300 + " a", "X" * 300 + " B", "x" * 300 + " c", "é" * 1500 + " long", "É" * 1500 + " long too",
]  # fmt: skip
TYPES = ("act", "regulation", "other", "judgment")
STATUSES = ("current", "repealed", "unknown", "historical")
LANGUAGES = ("en", "fr")
# These read the same in the English analysis of the section index, so the word rule tells what it finds in a
# text; tests/test_postgres_browse.py has those that do not, such as "no 12".
QUERIES = [None, "act", "ACT", "strasse", "straße", "12/2004", "cap 7", "about mining", "province cap 3", "spaced out",
           "out spaced", "text record", "record 3", "nothing at all", "min"]  # fmt: skip
FILTERS = [{}, {"type": "act"}, {"status": "repealed", "type": "regulation"}, {"access": "open"}, {"access": "metadata"},
           {"access": "all"}, {"year": "1995"}, {"year": "none"}, {"jurisdiction": "ng", "language": "en"}, {"language": "fr"},
           {"type": "act", "access": "open"}, {"type": "absent"}]  # fmt: skip
SORTS = ("relevance", "title", "newest", "oldest")


def in_database_order(engine, corpus):
    """The records as stored, in ID order as the database reads them: a sort that ties keeps it."""
    with engine.begin() as conn:
        order = {row[0]: n for n, row in enumerate(conn.execute(text("SELECT id FROM sources ORDER BY id")))}
    return sorted(Store(engine).load().sources, key=lambda s: order[s.id])


def reference(ordered, corpus, now, *, query=None, filters=None, sort="relevance", limit=20, offset=0):
    """The catalogue from the corpus as written: decide on each record, the word rule, Python's sort."""
    filters = {k: v for k, v in (filters or {}).items() if v is not None}
    rights = {r.source_id: r for r in corpus.rights}
    versions = {}
    for version in corpus.versions:
        versions.setdefault(version.source_id, []).append(version)
    candidates = []
    for source in ordered:
        record = rights.get(source.id)
        if not decide(record, now=now).metadata:
            continue
        readable = [v for v in versions.get(source.id, ()) if decide(record, v, now=now).full_text]
        rank = 0
        if query is not None:
            if has_every_word(source.title, query):
                rank = 3
            elif has_every_word(source.citation, query):
                rank = 2
            elif has_every_word(f"{source.title} {source.citation} {source.summary or ''}", query):
                rank = 1
            elif any(
                # A text's one part, as the section index holds what may be read at the time of writing.
                decide(record, v).full_text
                and has_every_word(f"{v.canonical_text} {source.title} {source.citation}", query)
                for v in versions.get(source.id, ())
            ):
                rank = 1
            else:
                continue
        values = {
            "jurisdiction": source.jurisdiction_id,
            "type": source.document_type,
            "language": source.language,
            "status": source.legal_status,
            "year": str(source.year) if source.year else None,
            "access": "open" if readable else "metadata",
        }
        candidates.append((rank, source, values))

    def selected(values, skip=None):
        return all(values[k] == v for k, v in filters.items() if k != skip)

    results = [c for c in candidates if selected(c[2])]
    facets = {name: {} for name in FACETS}
    for name in FACETS:
        for candidate in candidates:
            value = candidate[2][name]
            if value and selected(candidate[2], skip=name):
                facets[name][value] = facets[name].get(value, 0) + 1
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
        {
            name: sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            for name, counts in facets.items()
        },
    )


def answered(service, **request):
    limit, offset = request.get("limit", 20), request.get("offset", 0)
    page, facets = service.browse(
        query=request.get("query"),
        filters=request.get("filters"),
        sort=request.get("sort", "relevance"),
        limit=limit,
        offset=offset,
    )
    # A list keeps the order of a facet's values, which a reader sees.
    return (
        page.total,
        [s.id for s in page.items],
        {name: list(counts.items()) for name, counts in facets.items()},
    )


def assert_same(engine, corpus, now, requests):
    """Each request answered by the columns, by the walk over the records, and from the corpus as written."""
    ordered = in_database_order(engine, corpus)
    with engine.begin() as conn:
        repository = LegacyRepository(conn)
        service = LegacyCatalogue(repository)
        service.now = now
        assert repository.browsable()
        for request in requests:
            expected = reference(ordered, corpus, now, **request)
            assert answered(service, **request) == expected, request
        repository._has_gates = False
        assert not repository.browsable()
        for request in requests:
            expected = reference(ordered, corpus, now, **request)
            assert answered(service, **request) == expected, ("walk", request)


def add_varied(corpus, n, template, rng=None, **changes):
    """A record with its facts varied by its number, each title of TITLES in turn."""
    source, rights, versions = add_record(corpus, template, **changes)
    updated = source.model_copy(
        update={
            "title": TITLES[n % len(TITLES)],
            "citation": f"Cap {n}" if n % 3 else "Straße 7",
            # So a search for Straße finds titles (rank 3), citations (2) and summaries (1).
            "summary": "About mining and the Straße in the Northern Province." if n % 5 == 0 else None,
            "year": None if n % 7 == 0 else 1990 + n % 9,
            "document_type": TYPES[n % len(TYPES)],
            "legal_status": STATUSES[n % len(STATUSES)],
            "language": LANGUAGES[n % 11 == 0],
        }
    )
    corpus.sources[-1] = updated
    return updated, rights, versions


def save(engine, corpus):
    """Each text as one part, so that the section index holds it."""
    corpus.structure = [
        StructureNode(
            id=f"node-{v.id}", source_id=v.source_id, version_id=v.id, kind="section", locator="section/1", order=0,
            start_byte=0, end_byte=len(v.canonical_text.encode()), source_hash=v.content_hash,
        )
        for v in corpus.versions
    ]  # fmt: skip
    Store(engine).save(corpus)


@pytest.fixture
def corpus(cleared):
    corpus = cleared.model_copy(deep=True)
    corpus.structure, corpus.citations, corpus.amendments = [], [], []
    corpus.languages.append(Language(id="fr", name="French", tag="fr"))
    return corpus


@pytest.fixture
def varied(corpus, pg_engine):
    """Each thing decide asks, each on its own record, and the catalogue's facts varied across them all."""
    template = corpus.sources[0]
    n = 0
    for changes in RIGHTS_CHANGES:
        add_varied(corpus, n, template, rights_changes=changes.items())
        n += 1
    for changes in VERSION_CHANGES:
        add_varied(corpus, n, template, version_changes=changes.items())
        n += 1
    add_varied(corpus, n, template, approve=False)
    add_varied(corpus, n + 1, template, extra_texts=2)
    _, rights, versions = add_varied(corpus, n + 2, template, extra_texts=1)
    rights.approved_content_hashes.remove(versions[1].content_hash)  # one of its two approved
    for title_at in range(len(TITLES)):  # and every title may be read, so all of them can be sorted
        add_varied(corpus, title_at, template)
    save(pg_engine, corpus)
    return corpus


def test_the_columns_answer_as_the_policy_does_for_each_thing_it_asks(varied, pg_engine):
    requests = [
        {"query": query, "filters": filters, "sort": sort, "offset": offset}
        for query in QUERIES
        for filters in FILTERS
        for sort in SORTS
        for offset in (0, 20)
    ]
    assert_same(pg_engine, varied, ASKED, requests)
    # The fixture does tell them apart.
    rights = {r.source_id: r for r in varied.rights}
    public = [s for s in varied.sources if decide(rights[s.id], now=ASKED).metadata]
    readable = {v.source_id for v in varied.versions if decide(rights[v.source_id], v, now=ASKED).full_text}
    assert len(readable) < len(public) < len(varied.sources)


@pytest.mark.parametrize("now", [EARLIER, LATER, NOW, NOW - timedelta(days=1)], ids=str)
def test_the_same_at_any_time_of_asking(varied, pg_engine, now):
    requests = [
        {"filters": filters, "sort": sort}
        for filters in ({}, {"access": "open"}, {"access": "metadata"})
        for sort in SORTS
    ]
    assert_same(pg_engine, varied, now, requests)


def test_the_same_when_several_things_are_wrong_at_once(corpus, pg_engine):
    template = corpus.sources[0]
    rng = random.Random(46)
    for n in range(150):
        rights_changes = [i for c in rng.sample(RIGHTS_CHANGES, rng.randint(0, 3)) for i in c.items()]
        version_changes = [i for c in rng.sample(VERSION_CHANGES, rng.randint(0, 1)) for i in c.items()]
        add_varied(corpus, n, template, rights_changes=rights_changes, version_changes=version_changes,
                   extra_texts=rng.randint(0, 2))  # fmt: skip
    save(pg_engine, corpus)
    requests = [
        {"query": query, "filters": filters, "sort": sort, "offset": offset, "limit": rng.choice((1, 7, 20))}
        for query in (None, "act", "record 3", "text record")
        for filters in FILTERS
        for sort in SORTS
        for offset in (0, 13)
    ]
    assert_same(pg_engine, corpus, ASKED, requests)


def test_every_page_of_a_sort_holds_each_record_once_in_the_order_of_the_whole(varied, pg_engine):
    ordered = in_database_order(pg_engine, varied)
    with pg_engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        service.now = ASKED
        for sort in SORTS:
            total, whole, _ = reference(ordered, varied, ASKED, sort=sort, limit=10_000)
            pages = []
            for offset in range(0, total + 7, 7):
                page, _ = service.browse(sort=sort, limit=7, offset=offset)
                assert page.total == total
                pages += [s.id for s in page.items]
            assert pages == whole, sort
            assert len(set(pages)) == total


def test_the_sort_key_is_str_casefold_for_every_letter_there_is(pg_engine):
    cased = [
        chr(code)
        for code in range(1, sys.maxunicode + 1)
        if not 0xD800 <= code <= 0xDFFF
        and (
            chr(code).casefold() != chr(code)
            or chr(code).lower() != chr(code)
            or chr(code).upper() != chr(code)
        )
    ]
    assert len(cased) > 2500
    rng = random.Random(46)
    others = [chr(code) for code in rng.sample(range(0xE000, sys.maxunicode), 5000)] + list(
        map(chr, range(1, 128))
    )
    strings = cased + others + ["".join(cased[n : n + 40]) for n in range(0, len(cased), 40)]
    with pg_engine.begin() as conn:
        found = conn.execute(
            text(
                "SELECT catalogue_title_key(t) FROM unnest(CAST(:strings AS text[])) WITH ORDINALITY AS u(t, n) ORDER BY n"
            ),
            {"strings": strings},
        ).scalars()
        assert [(s, f) for s, f in zip(strings, found, strict=True) if s.casefold() != f] == []
    # The order of these strings is the order of their code points, as Python compares them.
    keys = sorted(s.casefold() for s in strings)
    with pg_engine.begin() as conn:
        ordered = conn.execute(
            text('SELECT k FROM unnest(CAST(:keys AS text[])) AS k ORDER BY k COLLATE "C"'), {"keys": keys}
        ).scalars()
        assert list(ordered) == keys


def test_a_title_as_long_as_it_likes_can_be_stored_and_sorted(corpus, pg_engine):
    template = corpus.sources[0]
    rng = random.Random(46)
    long = "".join(rng.choice("abcdefgéàßΣ") for _ in range(9000))  # no index entry could hold this
    titles = [long + " b", long.upper() + " A", long + " C", long[:200], long[:199]]
    for n, title in enumerate(titles):
        source, _, _ = add_record(corpus, template)
        corpus.sources[-1] = source.model_copy(update={"title": title, "citation": f"Cap {n}"})
    Store(pg_engine).save(corpus)
    assert_same(pg_engine, corpus, ASKED, [{"sort": sort} for sort in SORTS])


@pytest.mark.parametrize(
    ("table", "field"),
    [("sources", "id"), ("sources", "jurisdiction_id"), ("sources", "authority_id"), ("sources", "language"),
     ("rights", "id"), ("rights", "source_id"), ("versions", "id"), ("versions", "source_id"), ("versions", "language")],
)  # fmt: skip
def test_a_payload_that_disagrees_with_its_row_is_left_out(corpus, pg_engine, table, field):
    source, _, _ = add_record(corpus, corpus.sources[0], extra_texts=1)
    Store(pg_engine).save(corpus)
    where = {"sources": "id", "rights": "source_id", "versions": "source_id"}[table]
    with pg_engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        service.now = ASKED
        total = service.browse()[0].total
        opened = dict(service.browse()[1]["access"]).get("open", 0)
        conn.execute(
            text(
                f"UPDATE {table} SET payload = (payload::jsonb || jsonb_build_object('{field}', 'other'))::json"
                f" WHERE {where} = :source"
            ),
            {"source": source.id},
        )
        page, facets = service.browse(limit=100)
        assert page.total == total - (
            table != "versions"
        )  # the record itself stands if only its texts disagree
        assert (source.id in {s.id for s in page.items}) == (table == "versions")
        assert facets["access"].get("open", 0) == opened - 1


def test_a_request_reads_the_pages_records_and_no_text(varied, pg_engine):
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        service = LegacyCatalogue(repository)
        service.now = ASKED
        service.browse()  # what is learned once per request about the schema is not the point here
        event.listen(pg_engine, "before_cursor_execute", capture)
        try:
            for request in (
                {},
                {"query": "act"},
                {"query": "act", "filters": {"access": "open", "type": "act"}},
            ):
                del statements[:]
                page, _ = service.browse(sort="newest", **request)
                assert page.items
                if "query" not in request:  # a search asks the section index besides
                    assert (
                        len(statements) == 5
                    )  # how to plan, the counts, the page, its records, which are open
                reading = [s for s in statements if "payload" in s]
                assert len(reading) == 1 and "sources.id IN" in reading[0], reading
        finally:
            event.remove(pg_engine, "before_cursor_execute", capture)


def test_a_request_is_planned_for_its_values_however_often_it_is_asked(varied, pg_engine):
    # psycopg prepares a statement on its sixth run, and PostgreSQL may keep a plan blind to its values after
    # five more, which cannot hash a list of IDs: five times as slow at 50,000 records.
    with pg_engine.connect() as conn:
        for _ in range(13):
            with conn.begin():
                service = LegacyCatalogue(LegacyRepository(conn))
                service.now = ASKED
                assert conn.scalar(text("SELECT current_setting('plan_cache_mode')")) == "auto"
                first = answered(service, query="text record")
                assert conn.scalar(text("SELECT current_setting('plan_cache_mode')")) == "force_custom_plan"
                assert answered(service, query="text record") == first
                # What the text search set aside for its own statement ends with it.
                assert conn.scalar(text("SELECT current_setting('enable_seqscan')")) == "on"
                assert conn.scalar(text("SELECT current_setting('work_mem')")) != "32MB"
            with conn.begin():  # and the same list of IDs through /search
                service = LegacyCatalogue(LegacyRepository(conn))
                service.now = ASKED
                assert service.search("text record").total > 2
                assert conn.scalar(text("SELECT current_setting('plan_cache_mode')")) == "force_custom_plan"


def test_the_old_bounds_no_longer_apply(varied, pg_engine, monkeypatch):
    monkeypatch.setattr(api.catalogue, "MAX_TEXT_WORKS", 1)
    monkeypatch.setattr("api.repositories.BROWSE_BOUND", 1)
    ordered = in_database_order(pg_engine, varied)
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        monkeypatch.setattr(repository, "sources", lambda *a, **k: pytest.fail("walked the records"))
        service = LegacyCatalogue(repository)
        service.now = ASKED
        total, page, _ = answered(service, query="text record")
        assert total > 2  # more than either bound, found through the section index
        assert (total, page) == reference(ordered, varied, ASKED, query="text record")[:2]
        assert answered(service)[0] > 2
        # and the filter of /sources and /search reads no record either.
        assert service.sources(access="open", limit=100).total > 2
        assert service.search("text record", access="open").total > 2


def test_the_walk_keeps_its_bounds(varied, pg_engine, monkeypatch):
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        repository._has_gates = False
        service = LegacyCatalogue(repository)
        service.now = ASKED
        found = text_index.works(conn, "text record", text_index.Filters(), bound=None)
        assert len(found.rows) > 2
        monkeypatch.setattr(api.catalogue, "MAX_TEXT_WORKS", len(found.rows))
        service.browse(query="text record")
        monkeypatch.setattr(api.catalogue, "MAX_TEXT_WORKS", len(found.rows) - 1)
        with pytest.raises(ResultTooLarge):
            service.browse(query="text record")
        total = service.browse()[0].total
        monkeypatch.setattr("api.repositories.BROWSE_BOUND", total)
        service.browse()
        monkeypatch.setattr("api.repositories.BROWSE_BOUND", total - 1)
        with pytest.raises(ResultTooLarge):
            service.browse()


def test_a_text_is_searched_in_its_own_analysis_as_one_search_per_analysis_would(corpus, pg_engine):
    corpus.languages += [Language(id=tag, name=tag, tag=tag) for tag in ("pt", "es", "ar", "sw")]
    template = corpus.sources[0]
    texts = [
        ("en", "The commissions shall meet. An act of the minister."),
        ("fr", "Les commissions se réunissent. La loi du ministre."),
        ("pt", "As comissões reúnem-se. A lei do ministro."),
        ("es", "Las comisiones se reúnen. La ley del ministro."),
        ("ar", "اللجان تجتمع. قانون الوزير."),
        ("sw", "Tume zinakutana. Sheria ya waziri. The commissions."),
        ("sw", "Tume commiss."),  # the English reading of "commissions", in a text not read in English
    ]
    for tag, words in texts:
        digest = sha256(f"{words}\n".encode()).hexdigest()
        changes = {
            "language": tag,
            "canonical_text": f"{words}\n",
            "raw_hash": digest,
            "content_hash": digest,
        }
        add_record(corpus, template, version_changes=changes.items())
    save(pg_engine, corpus)
    queries = ["commissions", "commission", "commiss", "the", "les", "the les", "act", "shall meet", "minister", "ministre",
               "ministro", "ley", "comissões", "قانون", "الوزير", "tume", "qzxv", "o'brien", "a & b | !c", "(x", "x:y", "12/2004"]  # fmt: skip
    every = " OR ".join(
        f"(u.config = '{config}' AND (u.document || u.context) @@ plainto_tsquery('{config}', :q))"
        for config in text_index.ANALYSES
    )
    with pg_engine.begin() as conn:
        tables = (text_index._tables(conn)[0], text_index._documents(conn))
        answered_at_all = 0
        for query in queries:
            reading = " UNION ".join(f"SELECT u.work_id FROM {table} u WHERE {every}" for table in tables)
            expected = set(conn.scalars(text(reading), {"q": query}))
            found = text_index.works(conn, query, text_index.Filters(), bound=None)
            assert found.state == "ok" and set(found.rows) == expected and len(found.rows) == len(expected), (
                query
            )
            answered_at_all += bool(expected)
            within = text_index.works(conn, query, text_index.Filters(jurisdictions=("absent",)), bound=None)
            assert within.rows == []
        assert answered_at_all > 10  # the fixture does find things


def test_a_text_search_that_runs_out_of_time_says_so_and_the_catalogue_still_answers(
    varied, pg_engine, monkeypatch
):
    monkeypatch.setattr(text_index, "TIMEOUT_MS", 20)

    def slowly(conn, cursor, statement, parameters, context, executemany):
        if "@@ CAST(%(reading_0)s AS tsquery)" in statement:
            return "SELECT pg_sleep(5)", {}
        return statement, parameters

    event.listen(pg_engine, "before_cursor_execute", slowly, retval=True)
    try:
        with pg_engine.begin() as conn:
            service = LegacyCatalogue(LegacyRepository(conn))
            service.now = ASKED
            before = conn.scalar(text("SELECT current_setting('statement_timeout')"))
            page, _ = service.browse(query="act")
            assert page.notices == (api.catalogue.TEXT_NOT_SEARCHED["timeout"],) and page.total > 0
            # What it set aside for its own statement went with it.
            assert conn.scalar(text("SELECT current_setting('statement_timeout')")) == before
            assert conn.scalar(text("SELECT current_setting('enable_seqscan')")) == "on"
    finally:
        event.remove(pg_engine, "before_cursor_execute", slowly)


def api_answers(service, **request):
    found = service.sources(limit=100, **request)
    return found.total, [(s.id, s.has_full_text, s.access_mode) for s in found.items]


@pytest.mark.parametrize("access", ["all", "open", "metadata"])
def test_the_access_filter_of_sources_and_search_answers_as_the_policy_does(varied, pg_engine, access):
    rights = {r.source_id: r for r in varied.rights}
    versions = {}
    for version in varied.versions:
        versions.setdefault(version.source_id, []).append(version)

    def has_text(source):
        return any(decide(rights[source.id], v, now=ASKED).full_text for v in versions.get(source.id, ()))

    public = [s for s in varied.sources if decide(rights[s.id], now=ASKED).metadata]
    wanted = {
        "all": public,
        "open": [s for s in public if has_text(s)],
        "metadata": [s for s in public if not has_text(s)],
    }
    in_order = {s.id: n for n, s in enumerate(in_database_order(pg_engine, varied))}
    expected = sorted(wanted[access], key=lambda s: in_order[s.id])[:100]
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        service = LegacyCatalogue(repository)
        service.now = ASKED
        total, items = api_answers(service, access=access)
        assert total == len(wanted[access])
        assert [item[0] for item in items] == [s.id for s in expected]
        assert all(
            (text_ == has_text_of)
            for (_, text_, _), has_text_of in zip(items, map(has_text, expected), strict=True)
        )
        searched = service.search("act", access=access, limit=100)
        matches = [
            s for s in wanted[access] if has_every_word(f"{s.title} {s.citation} {s.summary or ''}", "act")
        ]
        assert searched.total == len(matches)
        assert {s.id for s in searched.items} == {
            s.id for s in sorted(matches, key=lambda s: in_order[s.id])[:100]
        }
        repository._has_gates = False
        assert api_answers(service, access=access) == (total, items)
        assert service.search("act", access=access, limit=100).total == searched.total


def test_withdrawing_a_record_or_its_review_expiring_takes_effect_on_the_next_answer(corpus, pg_engine):
    source, rights, versions = add_record(corpus, corpus.sources[0], (("expires_at", LATER),))
    other, other_rights, _ = add_record(corpus, corpus.sources[0])
    store = Store(pg_engine)
    store.save(corpus)
    ids = {source.id, other.id}

    def asked(now=ASKED):
        with pg_engine.begin() as conn:
            service = LegacyCatalogue(LegacyRepository(conn))
            service.now = now
            page, facets = service.browse(limit=100, filters={})
            found = {s.id: s.has_full_text for s in page.items if s.id in ids}
            return page.total, found, facets["access"].get("open", 0)

    total, found, opened = asked()
    assert found == {source.id: True, other.id: True}
    # The review ran out; nothing was written.
    assert asked(LATER) == (total, {source.id: False, other.id: True}, opened - 1)
    # One text is no longer approved.
    rights.approved_content_hashes = []
    store.save(corpus)
    assert asked() == (total, {source.id: False, other.id: True}, opened - 1)
    rights.approved_content_hashes = [versions[0].content_hash]
    store.save(corpus)
    assert asked() == (total, {source.id: True, other.id: True}, opened)
    # The record is withdrawn.
    other_rights.metadata_public = False
    store.save(corpus)
    assert asked() == (total - 1, {source.id: True}, opened - 1)


def test_the_answers_are_the_same_without_the_columns_and_after_they_return(varied, pg_engine):
    requests = [{"filters": filters, "sort": sort, "query": query} for filters in ({}, {"access": "open"}) for sort in SORTS
                for query in (None, "act")]  # fmt: skip
    ordered = in_database_order(pg_engine, varied)
    expected = [reference(ordered, varied, ASKED, **request) for request in requests]

    def answers():
        with pg_engine.begin() as conn:
            repository = LegacyRepository(conn)
            service = LegacyCatalogue(repository)
            service.now = ASKED
            return repository.browsable(), [answered(service, **request) for request in requests]

    assert answers() == (True, expected)
    config = Config("alembic.ini")
    with pg_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0045")
    assert answers() == (False, expected)
    with pg_engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
    assert answers() == (True, expected)
