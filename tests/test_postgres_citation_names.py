"""A citation's exact lookup from the index of migration 0048 (api.repositories.citation_candidates) finds what the
statement before it found and the caller kept: the same values, whatever their case, accents, ligatures or spaces, so
the same answers. What it no longer does is hand over every value that is not ASCII for the caller to throw away, which
cost more with each accented title and refused every lookup past MAX_UNPAGED_ROWS of them. The key is held to Python
for every code point there is, and nothing is read per record but the values that match."""

import random
import sys
import unicodedata
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_catalogue import TITLES

import api.repositories
from api import text_index
from api.catalogue import LegacyCatalogue
from api.repositories import LegacyRepository, ResultTooLarge
from api.search import normalize_citation
from api.store import Store, tables
from schema.models import Citation

pytestmark = pytest.mark.postgres

# What folds or spaces differently: a value and the others that read as it, and some that nearly do. Long ones are
# in TITLES; these are also held as a citation and as an alias.
VALUES = [
    *TITLES,
    "Loi n° 12/2004", "LOI N° 12/2004", "loi  n°\u00a012/2004", "Loi nº 12/2004", "loi no 12/2004",
    "Act\u30005", "Act\x855", "Act\u16805", "Act\u20285", "Act\u20295", "Act\u2003\u20095", "\u2003Act 5\u2003",
    "Act\t5", "Act\n5", "Act\x0b5", "Act\x0c5", "Act\r\n5", " act 5 ", "ACT 5", "act 5", "Act\u200b5", "é\x1c5",
    "É\x1f\x1e5", "é 5", "ﬁnance Act", "FINANCE ACT", "fi\u0301nance", "ﬁ\u0301nance", "\u212aelvin Act", "kelvin act",
    "Ⅷ Act", "viii act", "Café", "Cafe\u0301", "CAFÉ", "CAFE\u0301", "cafe", "\u1112\u1161\u11ab", "한",
    "① Act", "1 act", "½ Act", "1⁄2 act", "㎏ Act", "kg act", "ﷺ", "İzmir", "i\u0307zmir", "ı", "ς", "Σ", "σ",
    "\U0001f4dc Act", "𝐀𝐜𝐭 5", "𐐀 Act", "𐐨 act", "O'Brien v State", "100% Act", "a\\b", "a_b", "$1 Act",
    "Décret", "décret", "DÉCRET", "Arrêté du 5 mai", "ARRÊTÉ DU 5 MAI", "Lag om färdtjänst", "ʼn", "ŉ", "ǰ", "ǰ",
    "ß" * 40, "SS" * 40, "".join(random.Random(48).choices("abcdefghijklmnopqrstuvwxyz", k=9000)),
]  # fmt: skip
# How many of them read as these: each way of writing it, in TITLES as well.
ALIKE = {
    "act 5": 16,
    "décret": 3,
    "café": 4,
    "finance act": 5,
    "ss": 4,
    "한": 2,
    "loi n° 12/2004": 4,
    "kelvin act": 2,
}
# Nothing reads as these, and a statement that reads a word or a part of a value would find something.
NAMES_NOTHING = [
    "qzxv",
    "décret de rien",
    "act",
    "loi",
    "5",
    "cafe\u0301 ",
    "ß",
    "x" * 300,
    "é" * 1500,
    "a b",
]


def as_it_was(conn, term):
    """The statements before migration 0048, word for word but for the bound on rows: every value that is not ASCII
    comes back, whatever the term."""
    repository = LegacyRepository(conn)
    citations, sources = tables["citations"], tables["sources"]

    def may_be(value):
        normal = func.legacy_metadata_normalize(func.coalesce(value, "")) == term
        return or_(normal, func.octet_length(value) != func.length(value))

    aliases = conn.execute(
        select(citations.c.source_id, citations.c.payload).where(
            repository._identity_matches(citations), may_be(repository._field(citations, "value"))
        )
    ).all()
    own = conn.execute(
        select(sources.c.id).where(
            repository._identity_matches(sources),
            or_(may_be(repository._field(sources, "citation")), may_be(repository._field(sources, "title"))),
        )
    ).all()
    return [(row.source_id, row.payload) for row in aliases] + [(row.id, None) for row in own]


def kept(conn, candidates, term):
    """What LegacyCatalogue._named keeps of them, by the same comparison."""
    fields = {
        row.id: (row.citation, row.title)
        for row in conn.execute(
            text("SELECT id, payload ->> 'citation' AS citation, payload ->> 'title' AS title FROM sources")
        )
    }
    return [
        (source_id, alias)
        for source_id, alias in candidates
        if (alias is not None and normalize_citation(alias["value"]) == term)
        or (alias is None and term in map(normalize_citation, fields[source_id]))
    ]


def in_order(candidates):
    return sorted(candidates, key=lambda c: (c[0], c[1] is None, (c[1] or {}).get("id", "")))


def add_records(corpus, values, *, own=("title", "citation", "alias")):
    """A record for each value, held as each of the fields asked for; anything else a record says matches nothing."""
    base, rights = corpus.sources[1], corpus.rights[1]
    made = []
    for n, value in enumerate(values):
        changes = {
            "id": f"named-{len(corpus.sources):05d}",
            "title": f"Title {n}",
            "citation": f"Citation {n}",
        }
        for field in own:
            if field != "alias":
                changes[field] = value
        source = base.model_copy(update=changes)
        corpus.sources.append(source)
        corpus.rights.append(rights.model_copy(update={"id": f"rights-{source.id}", "source_id": source.id}))
        if "alias" in own:
            corpus.citations.append(Citation(id=f"alias-{source.id}", source_id=source.id, value=value))
        made.append(source)
    return made


def same_answers(conn, terms):
    """Each term: the statement as it was, as the caller keeps it, is what the lookup now hands over."""
    repository = LegacyRepository(conn)
    for term in terms:
        was = as_it_was(conn, term)
        now = repository.citation_candidates(term)
        assert repository._named_by_key
        assert in_order(now) == in_order(kept(conn, was, term)), ascii(term)
        assert all(c in was for c in now), ascii(term)


def code_points():
    return [code for code in range(1, sys.maxunicode + 1) if not 0xD800 <= code <= 0xDFFF]


def keys(conn, strings):
    return (
        conn.execute(
            text(
                "SELECT catalogue_name_key(t) FROM unnest(CAST(:strings AS text[])) WITH ORDINALITY AS u(t, n) ORDER BY n"
            ),
            {"strings": strings},
        )
        .scalars()
        .all()
    )


def differences(conn, strings):
    return [
        (ascii(s), ascii(k))
        for s, k in zip(strings, keys(conn, strings), strict=True)
        if normalize_citation(s) != k
    ]


def test_the_key_is_normalize_citation_for_every_code_point_there_is(pg_engine):
    """A newer Unicode in Python or PostgreSQL, whichever comes first, fails here rather than losing a citation."""
    codes = code_points()
    letters = list(map(chr, codes))
    with pg_engine.begin() as conn:
        for at in range(0, len(letters), 5000):
            batch = letters[at : at + 5000]
            whole = "x".join(batch)
            if keys(conn, [whole]) != [normalize_citation(whole)]:
                assert differences(conn, batch) == []
                assert differences(conn, ["x" + letter + "x" for letter in batch]) == []
                raise AssertionError(f"Code points from {ascii(batch[0])} differ only together")
        # At the ends of a value, and alone: where one that spaces or folds to nothing would show.
        spaced = [c for c in letters if c.isspace()]
        assert len(spaced) == 29
        assert differences(conn, spaced + [c + "x" + c for c in spaced] + [c + c for c in spaced]) == []
        folded = [c for c in letters if c.casefold() != c or unicodedata.normalize("NFKC", c) != c]
        assert len(folded) > 5000
        assert differences(conn, folded) == []


def test_the_key_composes_and_orders_as_normalize_citation_does(pg_engine):
    letters = list(map(chr, code_points()))
    marks = [c for c in letters if unicodedata.combining(c)]
    assert len(marks) > 900
    decomposable = [c for c in letters if unicodedata.decomposition(c)]
    assert len(decomposable) > 5000
    rng = random.Random(48)
    leading = [chr(c) for c in range(0x1100, 0x1113)]
    vowels = [chr(c) for c in range(0x1161, 0x1176)]
    trailing = [chr(c) for c in range(0x11A8, 0x11C3)]
    spaced = [c for c in letters if c.isspace()]
    folded = [c for c in letters if c.casefold() != c]
    pool = marks + decomposable + spaced * 40 + folded + list(map(chr, range(32, 127))) * 10
    strings = [
        *("a" + mark + "\u0323\u0301" for mark in marks),
        *("\u0301" + mark for mark in marks),
        *(unicodedata.normalize("NFD", c) for c in decomposable),
        *(unicodedata.normalize("NFKD", c) for c in decomposable),
        *(lead + vowel for lead in leading for vowel in vowels),
        *(lead + vowel + rng.choice(trailing) for lead in leading for vowel in vowels),
        *(unicodedata.normalize("NFC", lead + vowel) + end for lead in leading for vowel in vowels for end in trailing[:3]),
        *("".join(rng.choices(pool, k=rng.randint(1, 12))) for _ in range(30000)),
        "",
    ]  # fmt: skip
    with pg_engine.begin() as conn:
        assert differences(conn, strings) == []
        assert keys(conn, [None]) == [None]


@pytest.mark.parametrize("own", [("title",), ("citation",), ("alias",), ("title", "citation", "alias")])
def test_the_lookup_keeps_what_the_statement_before_it_kept(pg_engine, cleared, own):
    add_records(cleared, VALUES, own=own)
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        terms = {normalize_citation(v) for v in VALUES} - {""}
        terms |= {normalize_citation(s.title) for s in cleared.sources}
        terms |= {normalize_citation(s.citation) for s in cleared.sources}
        same_answers(conn, sorted(terms) + NAMES_NOTHING)
        repository = LegacyRepository(conn)
        for term in NAMES_NOTHING:
            assert repository.citation_candidates(term) == []
            assert LegacyRepository(conn).citation_candidates(term.upper() + "qzxv") == []
        # And the values that are alike are found together, so the agreement above is not of finding nothing.
        rows = 2 if own == ("title", "citation", "alias") else 1
        for term, alike in ALIKE.items():
            assert len(repository.citation_candidates(term)) == alike * rows, ascii(term)


def test_a_value_holds_one_field_of_a_record_and_the_other_says_nothing(pg_engine, cleared):
    made = add_records(cleared, ["Décret 7"], own=("title",)) + add_records(
        cleared, ["Act 7"], own=("citation",)
    )
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        same_answers(conn, ["décret 7", "act 7", "title 0", "citation 0", "qzxv"])
        repository = LegacyRepository(conn)
        assert repository.citation_candidates("décret 7") == [(made[0].id, None)]
        assert repository.citation_candidates("act 7") == [(made[1].id, None)]


def test_the_only_change_of_answer_is_a_value_python_always_read_as_spaced(pg_engine, cleared):
    """legacy_metadata_normalize took U+001C to U+001F for letters where a value was otherwise ASCII, and so never
    found it. normalize_citation takes them for spaces, as the key does and as SQLite always has. Every value here
    holds them, so nothing else moved."""
    [plain, accented] = add_records(cleared, ["Act\x1c5", "Décret\x1f5"], own=("alias",))
    # Beside an accented title the statement before this handed the record over anyway, and the caller found it.
    beside = add_records(cleared, ["Décret 9"], own=("title",))[0]
    cleared.sources[-1] = beside.model_copy(update={"citation": "Act\x1c9"})
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        assert kept(conn, as_it_was(conn, "act 5"), "act 5") == []
        assert [c[0] for c in repository.citation_candidates("act 5")] == [plain.id]
        same_answers(conn, ["décret 5", "act 9"])
        assert [c[0] for c in repository.citation_candidates("décret 5")] == [accented.id]
        assert repository.citation_candidates("act 9") == [(beside.id, None)]


def test_a_whole_citation_answer_is_the_same(pg_engine, cleared):
    """Through the caller, public and not: what it found and how, for each way of writing a value."""
    made = add_records(cleared, VALUES, own=("title", "citation", "alias"))
    hidden = cleared.rights[-1]
    cleared.rights[-1] = hidden.model_copy(update={"metadata_public": False})
    assert made[-1].id == hidden.source_id
    cleared.rights[-2] = cleared.rights[-2].model_copy(update={"status": "RESTRICTED"})
    # A record whose own name is not ASCII but whose alias is, and the other way about, so that the first candidate
    # the statement before this handed over for a source was not the one that names it.
    add_records(cleared, ["Décret vingt"], own=("title",))
    cleared.citations.append(
        Citation(id="alias-twenty", source_id=cleared.sources[-1].id, value="Decree Twenty")
    )
    add_records(cleared, ["Decree Thirty"], own=("title",))
    cleared.citations.append(
        Citation(id="alias-thirty", source_id=cleared.sources[-1].id, value="Décret trente")
    )
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        service = LegacyCatalogue(repository)
        before = LegacyRepository(conn)
        before.citation_candidates = lambda term: as_it_was(conn, term)
        was = LegacyCatalogue(before)
        was.now = service.now
        found = 0
        asked = VALUES + [v.upper() for v in VALUES] + [" ".join(v.split()) for v in VALUES]
        asked += [
            "Décret vingt",
            "decree twenty",
            "DECREE THIRTY",
            "décret trente",
            "qzxv",
            "Act 5 s. 3",
            "section 3",
        ]
        asked += [made[-1].title, made[-2].title]
        for value in asked:
            answer = service.citations(value)
            assert answer == was.citations(value), ascii(value)
            found += bool(answer.matches)
        assert found > len(VALUES)
        for private in (made[-1], made[-2]):
            assert not any(m.id == private.id for m in service.citations(private.title).matches)


def answer_without_refusing(conn, bound):
    assert len(as_it_was(conn, "qzxv")) >= bound + 5
    assert LegacyRepository(conn).citation_candidates("qzxv") == []
    assert LegacyCatalogue(LegacyRepository(conn)).citations("qzxv").matches == ()
    assert LegacyCatalogue(LegacyRepository(conn)).search_text("qzxv", text_index.Filters()).items == ()


def test_many_values_that_are_not_ascii_refuse_no_lookup(pg_engine, cleared):
    """The statement before this took every accent for a candidate, and refused past MAX_UNPAGED_ROWS of them."""
    bound = api.repositories.MAX_UNPAGED_ROWS
    add_records(cleared, ["Décret 1"], own=("title",))
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                f"""INSERT INTO sources (id, jurisdiction_id, authority_id, language, payload)
                SELECT 'many-' || n, jurisdiction_id, authority_id, language,
                    (payload::jsonb || jsonb_build_object('id', 'many-' || n, 'title', 'Arrêté ' || n))::json
                FROM sources, generate_series(1, {bound + 5}) AS n WHERE id = :id"""
            ),
            {"id": cleared.sources[-1].id},
        )
        answer_without_refusing(conn, bound)
        conn.execute(
            text(
                f"""INSERT INTO citations (id, source_id, payload)
                SELECT 'many-' || n, 'many-' || n, json_build_object(
                    'id', 'many-' || n, 'source_id', 'many-' || n, 'value', 'Décret n° ' || n)
                FROM generate_series(1, {bound + 5}) AS n"""
            )
        )
        answer_without_refusing(conn, bound)
        assert LegacyRepository(conn).citation_candidates("arrêté 7") == [("many-7", None)]
        [(source_id, alias)] = LegacyRepository(conn).citation_candidates("décret n° 7")
        assert source_id == "many-7" and alias["value"] == "Décret n° 7"


def test_the_bound_still_holds_for_values_that_do_read_as_the_term(pg_engine, cleared, monkeypatch):
    add_records(cleared, ["Décret 1"] * 3, own=("title",))
    add_records(cleared, ["Decree 2"] * 3, own=("citation",))
    add_records(cleared, ["Décret 3"] * 3, own=("alias",))
    add_records(cleared, ["Decree 4"] * 3, own=("alias",))
    Store(pg_engine).save(cleared)
    monkeypatch.setattr(api.repositories, "MAX_UNPAGED_ROWS", 2)
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        for term in ("décret 1", "decree 2", "décret 3", "decree 4"):
            with pytest.raises(ResultTooLarge):
                repository.citation_candidates(term)
    monkeypatch.setattr(api.repositories, "MAX_UNPAGED_ROWS", 3)
    with pg_engine.begin() as conn:
        repository = LegacyRepository(conn)
        for term in ("décret 1", "decree 2", "décret 3", "decree 4"):
            assert len(repository.citation_candidates(term)) == 3


@pytest.mark.parametrize("term", ["a\x00b", "\ud800"])
def test_a_term_no_database_can_hold_fails_as_it_did(pg_engine, cleared, term):
    """A NUL or a lone surrogate from a query is refused on the way to the database, by either statement."""
    Store(pg_engine).save(cleared)
    with pg_engine.connect() as conn, pytest.raises((DBAPIError, ValueError)) as was:
        as_it_was(conn, term)
    with pg_engine.connect() as conn, pytest.raises((DBAPIError, ValueError)) as now:
        LegacyRepository(conn).citation_candidates(term)
    assert type(now.value) is type(was.value)


def test_a_row_that_is_not_what_it_says_stays_out(pg_engine, cleared):
    add_records(cleared, ["Décret 1", "Decree 2"], own=("title", "alias"))
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE sources SET payload = (payload::jsonb || \'{"id": "elsewhere"}\')::json'
                " WHERE id = :id"
            ),
            {"id": cleared.sources[-1].id},
        )
        conn.execute(
            text(
                'UPDATE citations SET payload = (payload::jsonb || \'{"source_id": "elsewhere"}\')::json'
                " WHERE id = :id"
            ),
            {"id": cleared.citations[-1].id},
        )
        conn.execute(
            text(
                'UPDATE citations SET payload = (payload::jsonb || \'{"id": "elsewhere"}\')::json'
                " WHERE id = :id"
            ),
            {"id": cleared.citations[-2].id},
        )
        repository = LegacyRepository(conn)
        assert repository.citation_candidates("decree 2") == []
        assert [c[1] for c in repository.citation_candidates("décret 1")] == [None]
        same_answers(conn, ["decree 2", "décret 1"])


def test_of_two_aliases_naming_different_parts_the_first_in_the_table_comes_first(pg_engine, cleared):
    """The caller takes the first alias of a source that names a part. A scan reads them in the table's order, whatever
    path the planner takes, and whatever their IDs say."""
    source = add_records(cleared, ["Act"], own=("title",))[0]
    Store(pg_engine).save(cleared)
    ids = ["alias-z", "alias-m", "alias-a", "alias-q", "alias-b"]
    with pg_engine.begin() as conn:
        for n, alias in enumerate(ids):
            conn.execute(
                text(
                    "INSERT INTO citations (id, source_id, payload) VALUES (:id, :source, CAST(:payload AS json))"
                ),
                {
                    "id": alias,
                    "source": source.id,
                    "payload": Citation(
                        id=alias, source_id=source.id, value=f"Pénal {'' if n else ' '}"
                    ).model_dump_json(),
                },
            )
        for settings in ((), ("enable_seqscan",), ("enable_seqscan", "enable_bitmapscan")):
            for setting in settings:
                conn.execute(text(f"SET LOCAL {setting} = off"))
            found = LegacyRepository(conn).citation_candidates("pénal")
            assert [alias["id"] for _, alias in found] == ids, settings
            assert [alias["id"] for _, alias in as_it_was(conn, "pénal") if alias] == ids


def statements_of(engine, action):
    """What each statement of the lookup was planned as, by the planner as it stands."""
    plans = []

    def explain(conn, cursor, statement, parameters, context, executemany):
        if "catalogue_name_key(payload" in statement:
            with conn.connection.cursor() as probe:
                probe.execute("EXPLAIN (COSTS OFF) " + statement, parameters)
                plans.append("\n".join(row[0] for row in probe.fetchall()))

    event.listen(engine, "before_cursor_execute", explain)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", explain)
    return plans


@pytest.mark.parametrize("term", ["qzxv", "décret 7", "act 7", "alias 7"])
def test_the_lookup_reads_the_indexes_and_no_record_but_those_it_finds(pg_engine, cleared, term):
    add_records(cleared, ["Décret 7"], own=("title",))
    add_records(cleared, ["Act 7"], own=("citation",))
    add_records(cleared, ["Alias 7"], own=("alias",))
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO sources (id, jurisdiction_id, authority_id, language, payload)
                SELECT 'many-' || n, jurisdiction_id, authority_id, language, (payload::jsonb || jsonb_build_object(
                    'id', 'many-' || n, 'title', 'Title ' || n, 'citation', 'Citation ' || n))::json
                FROM sources, generate_series(1, 3000) AS n WHERE id = :id"""
            ),
            {"id": cleared.sources[-1].id},
        )
        conn.execute(
            text(
                """INSERT INTO citations (id, source_id, payload)
                SELECT 'many-' || n, 'many-' || n, json_build_object(
                    'id', 'many-' || n, 'source_id', 'many-' || n, 'value', 'Another ' || n)
                FROM generate_series(1, 3000) AS n"""
            )
        )
        conn.execute(text("ANALYZE sources"))
        conn.execute(text("ANALYZE citations"))
    with pg_engine.begin() as conn:
        plans = statements_of(pg_engine, lambda: LegacyRepository(conn).citation_candidates(term))
    assert len(plans) == 2
    aliases, own = plans
    assert "citations_name_value" in aliases and "Seq Scan" not in aliases, aliases
    assert "sources_name_title" in own and "sources_name_citation" in own and "Seq Scan" not in own, own


def test_a_write_is_read_at_once_and_a_withdrawal_takes_effect_on_the_next_answer(pg_engine, cleared):
    made = add_records(cleared, ["Décret 1"], own=("title", "alias"))[0]
    store = Store(pg_engine)
    store.save(cleared)

    def found(term):
        with pg_engine.begin() as conn:
            return [m.id for m in LegacyCatalogue(LegacyRepository(conn)).citations(term).matches]

    assert found("Décret 1") == [made.id]
    at = next(n for n, s in enumerate(cleared.sources) if s.id == made.id)
    cleared.sources[at] = made.model_copy(update={"title": "Arrêté 2"})
    cleared.citations[-1] = cleared.citations[-1].model_copy(update={"value": "Arrêté 3"})
    store.save(cleared)
    assert found("Arrêté 2") == [made.id] and found("ARRÊTÉ 3") == [made.id]
    assert found("Décret 1") == []
    rights = next(n for n, r in enumerate(cleared.rights) if r.source_id == made.id)
    cleared.rights[rights] = cleared.rights[rights].model_copy(update={"metadata_public": False})
    store.save(cleared)
    assert found("Arrêté 2") == [] and found("Arrêté 3") == []
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM citations WHERE source_id = :id"), {"id": made.id})
    cleared.rights[rights] = cleared.rights[rights].model_copy(update={"metadata_public": True})
    cleared.citations.pop()
    store.save(cleared)
    assert found("Arrêté 3") == [] and found("Arrêté 2") == [made.id]


def test_a_database_before_the_migration_compares_each_value_and_after_it_finds_the_same(pg_engine, cleared):
    made = add_records(cleared, VALUES, own=("title", "citation", "alias"))
    store = Store(pg_engine)
    store.save(cleared)
    terms = sorted({normalize_citation(v) for v in VALUES} - {""})

    def answers():
        with pg_engine.begin() as conn:
            service = LegacyCatalogue(LegacyRepository(conn))
            return {term: service.citations(term) for term in terms}

    def statements(conn, term):
        seen = []

        def note(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        event.listen(pg_engine, "before_cursor_execute", note)
        try:
            LegacyRepository(conn).citation_candidates(term)
        finally:
            event.remove(pg_engine, "before_cursor_execute", note)
        return seen

    now = answers()
    with pg_engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0047")
        assert conn.scalar(text("SELECT to_regprocedure('catalogue_name_key(text)')")) is None
        for index in ("sources_name_title", "sources_name_citation", "citations_name_value"):
            assert conn.scalar(text("SELECT to_regclass(:i)"), {"i": index}) is None
        repository = LegacyRepository(conn)
        for term in terms:
            assert in_order(repository.citation_candidates(term)) == in_order(as_it_was(conn, term))
        assert not repository._named_by_key
        assert any("octet_length" in s for s in statements(conn, "act 5"))
    assert answers() == now
    assert any(m.id == made[0].id for m in now[normalize_citation(VALUES[0])].matches)
    migrate(pg_engine)
    assert answers() == now
    with pg_engine.begin() as conn:
        assert LegacyRepository(conn).citation_candidates("qzxv") == []
        assert not any("octet_length" in s for s in statements(conn, "act 5"))
        # The planner knows at once that a value names few records.
        for index in ("sources_name_title", "sources_name_citation", "citations_name_value"):
            assert conn.scalar(text("SELECT count(*) FROM pg_stats WHERE tablename = :i"), {"i": index}) == 1
    # And before the function that compares them, as it was: all of it is handed over and the caller decides.
    with pg_engine.begin() as conn:
        conn.execute(text("DROP INDEX sources_name_title, sources_name_citation, citations_name_value"))
        conn.execute(text("DROP FUNCTION catalogue_name_key(text)"))
        conn.execute(text("DROP INDEX ix_sources_metadata_trigram"))
        conn.execute(text("DROP FUNCTION legacy_metadata_normalize(text)"))
        repository = LegacyRepository(conn)
        assert len(repository.citation_candidates("qzxv")) == len(cleared.sources) + len(cleared.citations)


def test_a_role_granted_the_tables_alone_can_look_up_and_write(pg_engine, cleared):
    """Writing a record evaluates the key, so whoever may write one must be able to call it."""
    made = add_records(cleared, ["Décret 1"], own=("title", "alias"))[0]
    Store(pg_engine).save(cleared)
    role = "names_" + uuid4().hex
    with pg_engine.begin() as conn:
        schema = conn.scalar(text("SELECT current_schema()"))
        conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        conn.execute(text(f'GRANT SELECT, UPDATE ON "{schema}".sources, "{schema}".citations TO "{role}"'))
    try:
        with pg_engine.begin() as conn:
            conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert LegacyRepository(conn).citation_candidates("décret 1")[1] == (made.id, None)
            conn.execute(
                text(
                    'UPDATE sources SET payload = (payload::jsonb || \'{"title": "Arrêté 2"}\')::json WHERE id = :id'
                ),
                {"id": made.id},
            )
            assert LegacyRepository(conn).citation_candidates("arrêté 2") == [(made.id, None)]
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


def test_a_value_is_read_as_the_payload_holds_it(pg_engine, cleared):
    """The index is of the payload as stored, so escapes and spaces in its JSON are read as Python reads them."""
    made = add_records(cleared, ["Act 1"], own=("title",))[0]
    Store(pg_engine).save(cleared)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE sources SET payload = (payload::jsonb || \'{"title": "D\\u00e9cret \\u00a0 2"}\')::json WHERE id = :id'
            ),
            {"id": made.id},
        )
        assert LegacyRepository(conn).citation_candidates("décret 2") == [(made.id, None)]
        assert LegacyRepository(conn).citation_candidates("act 1") == []
