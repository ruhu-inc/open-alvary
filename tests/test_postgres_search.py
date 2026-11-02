"""OA-S1, MIN-02 and D15: the section index follows what is public in the transaction that changes the
catalogue, and the text search answers ranked passages with exact snippets, telling no result apart
from a scope with no published text and from a search that could not run."""

import json
import math
import random
import re
import time
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, WORK, publish_act, withdraw

from api import text_index
from api.catalogue import TEXT_NOT_SEARCHED, LegacyCatalogue
from api.store import Store
from api.v1 import create_v1_app
from changes.record import namespace
from rights.database import provision_roles

pytestmark = pytest.mark.postgres


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


@pytest.fixture
def client(store, tmp_path):
    return TestClient(create_v1_app(store, tmp_path, lambda: []))


def units(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "text_unit")
        return [
            dict(row)
            for row in conn.execute(
                text(
                    f"SELECT version_id, start_byte, end_byte, node_id, kind, locator, pinpoint, legal_status,"
                    f" language, config FROM {table} ORDER BY version_id, start_byte"
                )
            ).mappings()
        ]


def search(client, q, **params):
    answer = client.get("/search/text", params={"q": q, **params})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_a_published_text_is_indexed_by_provision_and_a_withdrawn_one_is_not(store):
    _, version, nodes = publish_act(store, UNITS)
    found = units(store)
    assert {u["version_id"] for u in found} == {version.id} and {u["config"] for u in found} == {"english"}
    assert [u["pinpoint"] for u in found if u["pinpoint"]] == [
        "s. 1",
        "s. 2",
        "First Schedule",
        "First Schedule, para. 1",
    ]
    assert all(
        a["end_byte"] <= b["start_byte"] for a, b in zip(found, found[1:], strict=False)
    )  # never overlapping
    assert nodes["sec_1"].id in {u["node_id"] for u in found}
    withdraw(store)
    assert units(store) == []  # the same transaction that took the text out of view


def test_a_search_answers_ranked_passages_with_exact_snippets(store, client):
    _, version, nodes = publish_act(store, UNITS)
    raw = version.canonical_text.encode()
    page = search(client, "salary")
    assert page["status"] == "results" and page["total"] == 1 and page["total_exact"] is True
    [hit] = page["items"]
    assert hit["anchor_id"] == nodes["sec_1"].id and hit["citation"] == "Example Act, 2023, s. 1"
    assert hit["matched_by"] == "text" and 0 < hit["score"] <= 1 and hit["legal_status"] == "unknown"
    assert raw[hit["snippet_start_byte"] : hit["snippet_end_byte"]].decode() == hit["snippet"]
    assert [raw[a:b].decode() for a, b in hit["highlights"]] == ["salary"]
    assert hit["start_byte"] <= hit["snippet_start_byte"] <= hit["snippet_end_byte"] <= hit["end_byte"]
    assert hit["official_url"] == "https://www.nass.gov.ng/example-act.pdf" and hit["licence"].startswith(
        "Not"
    )
    assert page["coverage"] == {
        "records_in_scope": 1, "records_with_text": 1, "texts_searched": 1,
        "notice": "Only published texts are searched: 1 of the 1 records in this scope have one. Find records "
        "without a published text with /search.",
    }  # fmt: skip
    assert page["analysis"] == {"en": "english"} and page["ranking"] == "oa-rank-1" and page["index_built_at"]
    assert search(client, "lends")["items"][0]["anchor_id"] == nodes["sec_1"].id  # English stemming
    assert (
        search(client, '"from salary"')["total"] == 1
        and search(client, "salary -café")["status"] == "no_result"
    )
    assert search(client, "zebra")["status"] == "no_result"
    common = search(client, "the")
    assert common["status"] == "no_result" and "common words" in common["notices"][0]
    assert search(client, "salary", jurisdiction="rw")["status"] == "unsupported_scope"
    unsupported = search(client, "salary", type="regulation")
    assert unsupported["status"] == "unsupported_scope" and unsupported["coverage"]["texts_searched"] == 0
    # Golden queries (OA-S1): a statute by its short title, a section by number, a concept phrase.
    assert search(client, "Example Act")["items"][0]["kind"] == "document"  # its long title comes first
    assert nodes["sec_2"].id in [h["anchor_id"] for h in search(client, "Example Act section 2")["items"][:3]]
    assert search(client, "students fund lend")["items"][0]["anchor_id"] == nodes["sec_1"].id
    assert client.get("/search/text", params={"q": "salary", "cursor": "bogus"}).status_code == 400
    assert client.get("/search/text", params={"q": "x" * 2001}).status_code == 400


def test_a_citation_of_a_part_comes_first_and_is_not_repeated(store, client):
    _, version, nodes = publish_act(store, UNITS)
    page = search(client, "Example Act, 2023, s. 1(2)(b)")
    first, *rest = page["items"]
    assert first["matched_by"] == "citation" and first["anchor_id"] == nodes["sec_1__subsec_2__para_b"].id
    assert first["score"] is None and first["citation"] == "Example Act, 2023, s. 1(2)(b)"
    raw = version.canonical_text.encode()
    assert raw[first["snippet_start_byte"] : first["snippet_end_byte"]].decode() == first["snippet"]
    assert page["citation"]["provision"]["status"] == "resolved" and page["total"] == len(page["items"])
    assert all((h["start_byte"], h["end_byte"]) != (first["start_byte"], first["end_byte"]) for h in rest)
    second = search(client, "Example Act, 2023, s. 1(2)(b)", limit=1)
    assert [h["matched_by"] for h in second["items"]] == ["citation"] and second["next_cursor"]
    after = search(client, "Example Act, 2023, s. 1(2)(b)", limit=1, cursor=second["next_cursor"])
    assert after["items"][0]["matched_by"] == "text"


def test_pages_continue_without_repeating_a_passage(store, client):
    publish_act(store, UNITS)
    seen, cursor = [], None
    while True:
        page = search(client, "fund", limit=1, **({"cursor": cursor} if cursor else {}))
        seen += [(h["version_id"], h["start_byte"]) for h in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == page["total"] >= 2


def test_a_correction_of_the_record_reaches_the_index_and_a_repealed_text_scores_less(store, client):
    publish_act(store, UNITS)
    before = search(client, "salary")["items"][0]["score"]
    corpus = store.load()
    source = next(s for s in corpus.sources if s.id == WORK)
    source.title, source.legal_status = "Students Loans (Access) Act, 2023", "repealed"
    store.save(corpus)
    assert {u["legal_status"] for u in units(store)} == {"repealed"}
    renamed = search(client, "access salary")  # the new title, from the corrected record
    assert renamed["total"] == 1 and renamed["items"][0]["title"] == "Students Loans (Access) Act, 2023"
    assert search(client, "salary")["items"][0]["score"] == pytest.approx(
        before * text_index.NOT_IN_FORCE, rel=0.01
    )
    assert search(client, "salary", status="current")["status"] == "unsupported_scope"
    assert search(client, "salary", status="repealed")["total"] == 1


def test_the_final_schedule_of_a_long_statute_ranks_first_with_an_exact_excerpt(store, client):
    # MIN-02's acceptance: more than 16,383 words, and the final schedule is the only matching unit.
    sections = [
        {"kind": "paragraph", "text": f"{n}. The Minister may by order make provision for any matter under this "
         f"section {n} of the Act."}
        for n in range(1, 1000)  # the structure rules number sections up to 999
    ]  # fmt: skip
    schedule = [
        {"kind": "heading", "text": "SCHEDULES"},
        {"kind": "heading", "text": "First Schedule"},
        {"kind": "list_item", "marker": "1.", "text": "The quokka register is kept at the Lagos office."},
    ]
    _, version, nodes = publish_act(store, sections + schedule)
    words = len(version.canonical_text.split())
    assert words > 16_383 and all(
        u["end_byte"] - u["start_byte"] <= text_index.MAX_UNIT_BYTES for u in units(store)
    )
    started = time.perf_counter()
    page = search(client, "quokka")
    elapsed = time.perf_counter() - started
    assert page["total"] == 1 and page["items"][0]["anchor_id"] == nodes["sch_1__para_1"].id
    hit = page["items"][0]
    raw = version.canonical_text.encode()
    assert raw[hit["snippet_start_byte"] : hit["snippet_end_byte"]].decode() == hit["snippet"]
    assert "quokka" in hit["snippet"] and hit["citation"] == "Example Act, 2023, First Schedule, para. 1"
    busy = search(client, "minister order provision")
    assert busy["total"] == 999 and busy["next_cursor"] and elapsed < 3


def test_a_french_text_is_searched_in_french_and_other_languages_word_for_word(store, client):
    # The French search tests (MIN-02, LNG-01): verb and plural forms find each other, common words
    # alone find nothing, and accents are kept as written.
    publish_act(store, UNITS)
    french = [
        {"kind": "paragraph", "text": "1. Le Fonds prête aux étudiants inscrits dans les universités."},
        {"kind": "paragraph", "text": "2. Les données personnelles sont traitées loyalement."},
    ]
    publish_act(store, french, work="ng-loi-exemple-2023", title="Loi exemple, 2023", language="fr")
    page = search(client, "prêter")
    assert page["items"][0]["language"] == "fr" and page["analysis"] == {"en": "english", "fr": "french"}
    assert not any("Texts in fr" in n for n in page["notices"])  # French is no longer word for word
    assert search(client, "étudiant inscrit")["total"] == 1  # singular finds plural
    assert search(client, "donnée personnelle traiter")["items"][0]["citation"] == "Loi exemple, 2023, s. 2"
    common = search(client, "le la des du", language="fr")
    assert common["status"] == "no_result" and "common words" in common["notices"][0]
    assert search(client, "etudiants", language="fr")["status"] == "no_result"  # accents are kept
    kinyarwanda = [{"kind": "paragraph", "text": "1. Ikigega gitanga inguzanyo ku banyeshuri."}]
    publish_act(store, kinyarwanda, work="ng-itegeko-2023", title="Itegeko, 2023", language="rw")
    other = search(client, "inguzanyo")
    assert other["analysis"]["rw"] == "simple" and any(
        "Texts in rw are matched word for word" in n for n in other["notices"]
    )


def test_portuguese_spanish_and_arabic_texts_are_searched_by_their_stems(store, client):
    # The regional languages' search tests (MIN-02, LNG-02): plural and inflected forms find each other
    # through PostgreSQL's portuguese, spanish and arabic configurations; Portuguese and Spanish common
    # words alone find nothing, and Arabic has no list of them.
    publish_act(store, UNITS)
    portuguese = [
        {"kind": "paragraph", "text": "1. Os trabalhadores têm direito a contratos escritos."},
        {"kind": "paragraph", "text": "2. Os dados pessoais são tratados de forma lícita."},
    ]
    publish_act(store, portuguese, work="ng-lei-exemplo-2023", title="Lei exemplo, 2023", language="pt")
    page = search(client, "trabalhador contrato escrito")  # each word singular, the text's plural
    assert page["items"][0]["citation"] == "Lei exemplo, 2023, s. 1"
    assert page["analysis"]["pt"] == "portuguese" and not any("Texts in pt" in n for n in page["notices"])
    common = search(client, "os de a", language="pt")
    assert common["status"] == "no_result" and "common words" in common["notices"][0]
    spanish = [{"kind": "paragraph", "text": "1. Los trabajadores tienen derecho a vacaciones pagadas."}]
    publish_act(store, spanish, work="ng-ley-ejemplo-2023", title="Ley ejemplo, 2023", language="es")
    page = search(client, "trabajador vacación pagada", language="es")
    assert page["total"] == 1 and page["analysis"] == {"es": "spanish"}
    # "the employees" with its article and preposition, "the personal" and "the government": the
    # query's bare singular forms share their stems.
    arabic = [{"kind": "paragraph", "text": "1. يحق للموظفين الحصول على البيانات الشخصية لدى الحكومة."}]
    publish_act(store, arabic, work="ng-qanun-mithal-2023", title="قانون مثال، 2023", language="ar")
    page = search(client, "موظف شخصي حكومة")
    assert [h["language"] for h in page["items"]] == ["ar"] and page["analysis"]["ar"] == "arabic"
    assert not any("Texts in ar" in n for n in page["notices"])
    with store.engine.connect() as conn:  # word for word, the same query finds nothing
        simple = conn.scalar(
            text("SELECT to_tsvector('simple', :t) @@ websearch_to_tsquery('simple', :q)"),
            {"t": arabic[0]["text"], "q": "موظف شخصي حكومة"},
        )
    assert simple is False


def test_a_kiswahili_text_is_matched_word_for_word_and_says_so(store, client):
    # PostgreSQL has no analysis for Kiswahili (MIN-02's simple fallback, RSV-03): its words match as
    # written, a plural does not find its singular, and every answer says so.
    kiswahili = [{"kind": "paragraph", "text": "1. Wafanyakazi wana haki ya mikataba ya maandishi."}]
    publish_act(store, kiswahili, work="ng-sheria-mfano-2023", title="Sheria ya Mfano, 2023", language="sw")
    page = search(client, "mikataba")
    assert page["total"] == 1 and page["analysis"] == {"sw": "simple"}
    assert page["notices"] == [
        "Texts in sw are matched word for word (no stemming or common words) until the language passes "
        "the search tests."
    ]
    assert search(client, "mkataba")["status"] == "no_result"  # the singular of mikataba


def test_a_date_searches_only_texts_in_force_on_it(store, client):
    publish_act(store, UNITS, effective_from=date(2023, 6, 1))
    assert search(client, "salary", as_of="2024-01-01")["total"] == 1
    assert search(client, "salary", as_of="2020-01-01")["status"] == "unsupported_scope"


def test_an_index_not_yet_built_says_so_and_the_worker_builds_it(store, client):
    publish_act(store, UNITS)
    with store.engine.begin() as conn:
        state = namespace(conn).qualified(conn, "changes", "text_index_state")
        conn.execute(text(f"UPDATE {state} SET built = false"))
    page = search(client, "salary")
    assert page["status"] == "degraded" and page["items"] == [] and "being built" in page["notices"][0]
    with store.engine.begin() as conn:
        assert text_index.ensure(conn) is True and text_index.ensure(conn) is False
    assert search(client, "salary")["status"] == "results"


def test_a_text_withdrawn_while_the_answer_is_sent_is_refused(store, client, monkeypatch):
    publish_act(store, UNITS)
    monkeypatch.setattr(LegacyCatalogue, "readable", lambda self, version_id: None)
    refused = client.get("/search/text", params={"q": "salary"})
    assert refused.status_code == 503 and refused.json()["code"] == "temporarily_unavailable"


def test_the_mcp_tools_search_texts_and_resolve_a_pinpoint(store, tmp_path):
    from api.main import create_app

    _, _, nodes = publish_act(store, UNITS)
    client = TestClient(create_app(store, tmp_path))

    def call(name, arguments):
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": name, "arguments": arguments},
        }
        return client.post("/mcp", json=body).json()["result"]

    found = call("search_text", {"query": "salary", "limit": 3})
    assert not found["isError"] and nodes["sec_1"].id in found["content"][0]["text"]
    resolved = call("resolve_citation", {"citation": "Example Act, 2023, s. 1(2)(b)"})
    assert not resolved["isError"] and nodes["sec_1__subsec_2__para_b"].id in resolved["content"][0]["text"]
    invalid = {"name": "search_text", "arguments": {"query": "salary", "as_of": "yesterday"}}
    answer = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": 2, "params": invalid})
    assert answer.json()["error"]["code"] == -32602


def test_record_search_reads_whole_words_from_the_indexes_on_every_entry_point(store, client, tmp_path):
    """G11: /search, the website's search and MCP search_catalogue find a record by the words of its
    title and citation (the word index on sources) and of one part of its published text (the section
    index), never by part of a word, and no statement they send compares by substring."""
    from api.main import create_app
    from api.site import create_site_app

    publish_act(store, UNITS)
    legacy, site = TestClient(create_app(store, tmp_path)), TestClient(create_site_app(store, tmp_path))
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    def found(q):
        tool = {"name": "search_catalogue", "arguments": {"query": q}}
        reply = legacy.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": tool})
        answers = [
            client.get("/search", params={"q": q}).json(),
            legacy.get("/search", params={"q": q}).json(),
            site.get("/search", params={"q": q}).json(),  # the website's, at /site/v1/search
            json.loads(reply.json()["result"]["content"][0]["text"]),
        ]
        ids = {frozenset(s["id"] for s in answer["items"]) for answer in answers}
        assert len(ids) == 1 and not any("notices" in answer for answer in answers), (q, answers)
        return set(ids.pop())

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        assert found("example act") == found("2023 EXAMPLE") == {WORK}  # its title and citation
        assert found("salary") == {WORK}  # only in its text
        assert found("lends students") == {WORK}  # the text's English analysis, in one section
        assert found("sal") == found("exam") == set()  # never part of a word (a stem may match)
        assert found("salary lagos") == set()  # in two parts of the text, never one
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    assert statements and not [
        s for s in statements if re.search(r"(?i)\b(i?like|similar to|strpos|position)\b", s)
    ]
    withdraw(store)  # its text stops being public: the words of the text no longer find it
    assert found("salary") == set() and found("example act") == {WORK}
    with store.engine.begin() as conn:
        state = namespace(conn).qualified(conn, "changes", "text_index_state")
        conn.execute(text(f"UPDATE {state} SET built = false"))
    page = client.get("/search", params={"q": "example"}).json()
    assert page["total"] == 1 and page["notices"] == [TEXT_NOT_SEARCHED["building"]]


def test_the_integration_acceptance_checks_pass_on_a_published_text(store, client, capsys):
    from operations import acceptance

    publish_act(store, UNITS)
    golden = [
        {"query": "salary", "expect": ["s. 1"]},
        {"query": "Lagos office", "expect": ["First Schedule, para. 1"]},
    ]
    report = acceptance.run(client, "Example Act, 2023, s. 1(2)(b)", golden)
    assert not report.failed and [r[1] for r in report.results].count("PASS") == 10
    missing = acceptance.run(client, "Example Act, 2023, s. 9", golden)
    assert missing.failed and missing.results[0][:2] == ("OA-S2 provision lookup", "FAIL")


# --- the same hits in the same order as scoring every match ------------------------------------------

# The search as it was before it read each reading once and left out units that cannot come first.
THEN_MATCH = " OR ".join(
    f"(u.config = '{c}' AND (u.document || u.context) @@ websearch_to_tsquery('{c}', :q))"
    for c in text_index.ANALYSES
)
THEN_IN_FORCE = (
    "CASE WHEN u.replaced OR u.legal_status IN ('repealed', 'superseded', 'historical') THEN 0.8 ELSE 1.0 END"
)
THEN_SCORE = (
    "ts_rank_cd(CAST('{0.1, 0.2, 0.4, 1.0}' AS float4[]), u.document || u.context,"
    f" websearch_to_tsquery(CAST(u.config AS regconfig), :q), 32) * {THEN_IN_FORCE}"
)


def then_weighted(conn, table, query):
    """The any-term arm's match, score and parameters, as they were."""
    total = conn.scalar(
        text("SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass(:t)"), {"t": table}
    )
    if not total:
        total = conn.scalar(text(f"SELECT count(*) FROM {table}")) or 0
    matches, scores, params = [], [], {}
    for config in text_index.ANALYSES:
        if not conn.scalar(
            text(f"SELECT EXISTS (SELECT 1 FROM {table} u WHERE u.config = :c)"), {"c": config}
        ):
            continue
        lexemes = conn.scalars(
            text("SELECT unnest(tsvector_to_array(to_tsvector(CAST(:c AS regconfig), :q)))"),
            {"c": config, "q": query},
        ).all()[:12]
        if not lexemes:
            continue
        parts, keys = [], []
        for n, lexeme in enumerate(sorted(lexemes)):
            key = f"lx_{config}_{n}"
            params[key] = "'" + lexeme.replace("'", "''") + "'"
            keys.append(key)
            df = conn.scalar(
                text(
                    f"SELECT count(*) FROM (SELECT 1 FROM {table} u WHERE u.config = :c"
                    f" AND (u.document || u.context) @@ CAST(:lx AS tsquery) LIMIT 50000) AS d"
                ),
                {"c": config, "lx": params[key]},
            )
            idf = math.log(1 + (max(total, df) - df + 0.5) / (df + 0.5))
            parts.append(
                f"{idf:.6f} * ts_rank_cd(CAST('{{0.1, 0.2, 0.4, 1.0}}' AS float4[]), u.document || u.context,"
                f" CAST(:{key} AS tsquery), 1)"
            )
        params[f"any_{config}"] = " | ".join(params[k] for k in keys)
        matches.append(
            f"(u.config = '{config}' AND (u.document || u.context) @@ CAST(:any_{config} AS tsquery))"
        )
        scores.append(f"WHEN '{config}' THEN {' + '.join(parts)}")
    if not matches:
        return None
    score = f"(CASE u.config {' '.join(scores)} ELSE 0 END) * {THEN_IN_FORCE}"
    return " OR ".join(matches), score, params


def as_it_was(conn, query, filters, *, limit, offset, exclude=None, any_terms=False, bound=None):
    """What the search answered when it scored every match: (rows, total, exact), or None with no terms."""
    table, _ = text_index._tables(conn)
    where, params = filters.where()
    params |= {"q": query}
    if exclude:
        where += " AND NOT (u.version_id = :x_version AND u.start_byte = :x_start AND u.end_byte = :x_end)"
        params |= dict(zip(("x_version", "x_start", "x_end"), exclude, strict=True))
    match, score = THEN_MATCH, THEN_SCORE
    if any_terms:
        weighted = then_weighted(conn, table, query)
        if weighted is None:
            return None
        match, score, extra = weighted
        params |= extra
    bound = bound or text_index.COUNT_BOUND
    total = conn.scalar(
        text(
            f"SELECT count(*) FROM (SELECT 1 FROM {table} u WHERE ({match}) AND {where} LIMIT {bound + 1}) AS matched"
        ),
        params,
    )
    rows = conn.execute(
        text(
            f"SELECT {text_index._COLUMNS}, {score} AS score FROM {table} u WHERE ({match}) AND {where}"
            " ORDER BY score DESC, u.version_id, u.start_byte LIMIT :limit OFFSET :offset"
        ),
        params | {"limit": limit, "offset": offset},
    ).all()
    return rows, min(total, bound), total <= bound


FILLER = {
    "en": "fund loan student office register notice officer tribunal appeal".split(),
    "fr": "fonds prêt étudiant bureau registre avis tribunal appel".split(),
    "pt": "fundo empréstimo estudante escritório registo aviso tribunal recurso".split(),
    "es": "fondo préstamo estudiante oficina registro aviso tribunal recurso".split(),
    "ar": "صندوق قرض طالب مكتب سجل إشعار محكمة".split(),
    "sw": "mfuko mkopo mwanafunzi ofisi daftari taarifa mahakama".split(),
}
COMMON = {
    "en": ("shall", "commission", "commissions", "minister", "act"),
    "fr": ("commission", "commissions", "ministre", "loi", "shall"),
    "pt": ("comissão", "comissões", "ministro", "lei", "act"),
    "es": ("comisión", "comisiones", "ministro", "ley", "act"),
    "ar": ("قانون", "الوزير", "commission"),
    "sw": ("sheria", "waziri", "commission", "shall", "act"),
}
# Often none, sometimes enough for every band, and more places than a vector keeps for one word.
TIMES = (0, 0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 4, 5, 6, 7, 8, 9, 12, 17, 33, 64, 300)
PLACES = ("en", "en", "en", "en-NG", "fr", "fr", "pt", "es", "ar", "sw")
STATUSES = ("current", "current", "unknown", "repealed", "superseded", "historical")


def varied_units(conn, rng, *, texts=60):
    """Units as the index writes them, varied in all a search can tell apart. A version's ID sorts apart from its
    number (the order of ties), some of them in a text's different case."""
    table, state = text_index._tables(conn)
    documents = text_index._documents(conn)
    versions, rows, document_rows = [], [], []
    for at in range(texts):
        language = rng.choice(PLACES)
        base = language.split("-")[0]
        work = f"{rng.choice('ngkerw')}-work-{at % (texts - 7)}"
        version = rng.choice(("v-", "V-", "v_")) + format(rng.randrange(10**6), "06d") + f"-{at}"
        common = COMMON[base]
        facts = {
            "work_id": work,
            "language": language,
            "config": text_index.config_for(language),
            "analysis": text_index.config_for(language),
            "jurisdiction_id": rng.choice(("ng", "ke", "rw")),
            "document_type": rng.choice(("act", "regulation", "judgment")),
            "legal_status": rng.choice(STATUSES),
            "year": rng.choice((None, 1999, 2004, 2023)),
            "effective_from": rng.choice((None, date(2000, 1, 1), date(2024, 1, 1))),
            "effective_until": rng.choice((None, None, date(2025, 1, 1))),
            "content_hash": "a" * 64,
            "prior_version_id": None,
            "work_hash": "b" * 64,
            "version_id": version,
        }
        title = " ".join(rng.choices((*common, *FILLER[base]), k=rng.randint(1, 4)))
        citation = f"{rng.choice(common)} {at}"
        start = 0
        for n in range(rng.choice((1, 2, 3, 8, 40))):
            words = [rng.choice(FILLER[base]) for _ in range(rng.randint(1, 30))]
            for word in common:
                words += [word] * rng.choice(TIMES)
            rng.shuffle(words)
            words = " ".join(words)
            end = start + len(words.encode())
            heading = " ".join(rng.choices((*common, *FILLER[base]), k=rng.randint(0, 3)))
            cited = f"s. {n}" if n % 3 else None
            rows.append(
                facts
                | {
                    "start_byte": start,
                    "end_byte": end,
                    "node_id": f"{version}-{n}",
                    "kind": rng.choice(("section", "paragraph", "order")),
                    "locator": f"sec_{n}",
                    "pinpoint": cited,
                    "heading": heading,
                    "headings": heading,
                    "words": words,
                    "title": title,
                    "identifiers": text_index.identifiers(citation, cited),
                }
            )
            start = end + 1
        versions.append(version)
        document_rows.append(
            facts
            | {
                "start_byte": 0,
                "end_byte": 40,
                "node_id": None,
                "names": [title],
                "truncated": False,
                "title": title,
                "words": " ".join(rng.choices((*common, *FILLER[base]), k=5)),
                "outline": " ".join(rng.choices((*common, *FILLER[base]), k=8)),
                "identifiers": citation,
            }
        )
    conn.execute(text(text_index.INSERT.format(t=table)), rows)
    conn.execute(text(text_index.DOCUMENT_INSERT.format(t=documents)), document_rows)
    conn.execute(text(f"UPDATE {table} SET replaced = true WHERE version_id = ANY(:v)"), {"v": versions[::7]})
    conn.execute(
        text(f"UPDATE {documents} SET replaced = true WHERE version_id = ANY(:v)"), {"v": versions[::7]}
    )
    conn.execute(
        text(f"UPDATE {state} SET built = true, analyzer = :a, built_at = statement_timestamp()"),
        {"a": text_index.ANALYZER},
    )
    conn.execute(text(f"ANALYZE {table}"))
    return versions, sorted({row["work_id"] for row in rows})


@pytest.fixture
def varied(pg_engine):
    migrate(pg_engine)
    with pg_engine.begin() as conn:
        versions, works = varied_units(conn, random.Random(20261102))
    return pg_engine, versions, works


QUERIES = (
    # One word: in each analysis alike, its readings apart, and in most units, in some, in none.
    "shall", "act", "commission", "commissions", "minister", "ministre", "ministro", "قانون", "sheria", "SHALL",
    "fund", "lagos", "qzxv", "s", "2023",
    # Several, with the search syntax a reader may type.
    "shall act", "shall act minister", "commission minister", '"shall act"', '"act shall"', "act or fund",
    "act -shall", "-shall", "shall -shall", "o'brien", "data-protection", "a & b | !c", "(x", "x:y", "12/2004",
    # Common words of some analyses and not others, and of all.
    "the", "les", "the les", "the shall", "the and", "the -and", "!!",
)  # fmt: skip


def filters_for(versions, works):
    return (
        text_index.Filters(),
        text_index.Filters(jurisdictions=("ng",)),
        text_index.Filters(jurisdictions=("ng", "ke"), admitted=(works[0], works[-1])),
        text_index.Filters(jurisdictions=()),
        text_index.Filters(language="en"),
        text_index.Filters(language="en-ng"),
        text_index.Filters(types=("act", "judgment")),
        text_index.Filters(statuses=("repealed", "historical")),
        text_index.Filters(as_of=date(2024, 6, 1)),
        text_index.Filters(year_from=2000, year_to=2023),
        text_index.Filters(source_id=works[3]),
        text_index.Filters(version_id=versions[5]),
        text_index.Filters(kinds=("section", "order")),
        text_index.Filters(jurisdictions=("ke",), types=("act",), statuses=("current",), year_from=2000),
    )


PAGES = ((25, 0), (5, 0), (1, 0), (0, 0), (5, 3), (3, 25), (100, 0), (100, 1000))


def same_as_it_was(conn, query, filters, limit, offset, *, exclude=None, any_terms=False, bound=None):
    found = text_index.search(
        conn, query, filters, limit=limit, offset=offset, exclude=exclude, any_terms=any_terms
    )
    then = as_it_was(
        conn, query, filters, limit=limit, offset=offset, exclude=exclude, any_terms=any_terms, bound=bound
    )
    asked = (query, filters, limit, offset, exclude, any_terms)
    terms = dict(
        conn.execute(
            text(
                "SELECT c, numnode(websearch_to_tsquery(CAST(c AS regconfig), :q)) > 0"
                " FROM unnest(CAST(:configs AS text[])) AS c"
            ),
            {"q": query, "configs": list(text_index.ANALYSES)},
        ).all()
    )
    assert found.common_only == tuple(c for c, read in sorted(terms.items()) if not read), asked
    if then is None:
        assert found.state == "no_terms", asked
        return found
    rows, total, exact = then
    if found.state == "no_terms":  # every analysis read only common words: nothing can match
        assert rows == [] and total == 0, asked
        return found
    assert found.state == "ok", asked
    assert [dict(r._mapping) for r in found.rows] == [dict(r._mapping) for r in rows], asked
    assert (found.total, found.exact) == (total, exact), asked
    return found


def test_the_same_hits_in_the_same_order_as_scoring_every_match(varied):
    engine, versions, works = varied
    answered = 0
    with engine.begin() as conn:
        for query in QUERIES:
            for filters in filters_for(versions, works):
                answered += len(same_as_it_was(conn, query, filters, 25, 0).rows)
            for limit, offset in PAGES:
                answered += len(same_as_it_was(conn, query, text_index.Filters(), limit, offset).rows)
    assert answered > 3000  # the fixture does find things


@pytest.mark.parametrize("bound", [10, 40, 200])
def test_the_same_hits_when_most_units_are_left_unscored(varied, monkeypatch, bound):
    # Past the count's bound a search for one word reads the units where it weighs most. A small bound puts every
    # filter and page of this small fixture there, so each band gets to answer and each gets to fail.
    engine, versions, works = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", bound)
    bands, every_match = [], 0

    def capture(conn, cursor, statement, parameters, context, executemany):
        nonlocal every_match
        if "unit_heavy_words" in statement:
            bands.append(parameters["heavy_0"][-1].rsplit(" ", 1)[1])
        elif "AS found ORDER BY score DESC" in statement:  # the search's own, not as_it_was's
            every_match += 1

    with engine.begin() as conn:
        event.listen(engine, "before_cursor_execute", capture)
        try:
            for query in (
                "shall",
                "act",
                "commission",
                "commissions",
                "minister",
                "ministre",
                "قانون",
                "fund",
            ):
                for filters in filters_for(versions, works):
                    for limit, offset in PAGES:
                        same_as_it_was(conn, query, filters, limit, offset, bound=bound)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert set(bands) == set(text_index.BANDS) and every_match


def test_a_page_of_a_common_word_scores_only_the_units_it_could_come_from(varied, monkeypatch):
    engine, _, _ = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", 40)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "ORDER BY score DESC" in statement:
            statements.append(statement)

    with engine.begin() as conn:
        event.listen(engine, "before_cursor_execute", capture)
        try:
            found = text_index.search(conn, "shall", text_index.Filters(), limit=5, offset=0)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(found.rows) == 5 and statements and all("unit_heavy_words" in s for s in statements)
        assert [
            r.start_byte for r in same_as_it_was(conn, "shall", text_index.Filters(), 5, 0, bound=40).rows
        ]
        # What was scored is what could come first: a word's weight in the unit is all there is to its score.
        table, _ = text_index._tables(conn)
        rows = conn.execute(
            text(
                f"SELECT u.version_id, u.start_byte, ts_rank_cd(CAST('{text_index.WEIGHTS}' AS float4[]),"
                f" u.document || u.context, CAST(:word AS tsquery), 32) AS rank,"
                f" (SELECT sum(CASE p WHEN 'A' THEN 1.0 WHEN 'B' THEN 0.4 WHEN 'C' THEN 0.2 ELSE 0.1 END)"
                f" FROM unnest(u.document || u.context) AS w(lexeme, positions, weights), unnest(w.weights) AS p"
                f" WHERE w.lexeme = :lexeme) AS weight FROM {table} u"
                f" WHERE (u.document || u.context) @@ CAST(:word AS tsquery)"
            ),
            {"word": "'shall'", "lexeme": "shall"},
        ).all()
        assert len(rows) > 40
        for row in rows:
            assert row.rank == pytest.approx(float(row.weight) / (float(row.weight) + 1), rel=1e-6), row


def test_every_word_of_every_unit_is_listed_in_the_band_of_its_score(varied):
    # The search leaves out a unit on this index's word alone, so it must hold for every word there is, in every
    # analysis, with ts_rank_cd itself as the judge: at the bands' edges, and past what a vector keeps for one word.
    engine, _, _ = varied
    with engine.begin() as conn:
        table, _ = text_index._tables(conn)
        heavy = namespace(conn).qualified(conn, "changes", "unit_heavy_words")
        rows = conn.execute(
            text(
                f"SELECT w.lexeme, ts_rank_cd(CAST('{text_index.WEIGHTS}' AS float4[]), u.document || u.context,"
                f" CAST(quote_literal(w.lexeme) AS tsquery), 32) AS rank, {heavy}(u.document || u.context) AS listed"
                f" FROM {table} u CROSS JOIN LATERAL unnest(u.document || u.context) AS w(lexeme, positions, weights)"
                " WHERE u.ctid IN (SELECT ctid FROM (SELECT ctid, row_number() OVER () AS n"
                f" FROM {table}) AS t WHERE n % 7 = 0)"
            )
        ).all()
    assert len(rows) > 500
    bands = {}
    for row in rows:
        listed = [t.rsplit(" ", 1)[1] for t in row.listed if t.rsplit(" ", 1)[0] == row.lexeme]
        assert len(listed) <= 1, row
        weight = row.rank / (1 - row.rank)  # the score is weight / (weight + 1)
        expected = next((b for b in text_index.BANDS if weight >= float(b) * (1 - 1e-6)), None)
        assert (listed[0] if listed else None) == expected, (row.lexeme, weight, listed)
        bands[expected] = bands.get(expected, 0) + 1
    assert set(bands) == {None, *text_index.BANDS}  # each band is there to be held


def test_a_word_at_the_edge_of_each_band_is_listed_in_it(pg_engine):
    # Every weight up to past the last band, in tenths, from every mix of places: ts_rank_cd itself says what the
    # weight is, and the index must have it in the band that weight falls in, or none below the first.
    migrate(pg_engine)
    rng = random.Random(20261102)
    worth = {"A": 10, "B": 4, "C": 2, "D": 1}  # in tenths
    bands = [(int(float(band) * 10), band) for band in text_index.BANDS]
    with pg_engine.begin() as conn:
        heavy = namespace(conn).qualified(conn, "changes", "unit_heavy_words")
        for tenths in range(1, 141):
            for letters in ("D", "CD", "BD", "AD", "ABCD", "BC"):
                places = {}
                left = tenths
                while left:  # as much as fits of each, in turn, down to the D or C that ends it exactly
                    fits = [letter for letter in letters if worth[letter] <= left]
                    if not fits:
                        break
                    letter = rng.choice(fits)
                    places[letter] = places.get(letter, 0) + 1
                    left -= worth[letter]
                if left:
                    continue
                vector = " || ".join(
                    f"setweight(to_tsvector('simple', repeat('word ', {n})), '{letter}')"
                    for letter, n in sorted(places.items())
                )
                rank, listed = conn.execute(
                    text(
                        f"SELECT ts_rank_cd(CAST('{text_index.WEIGHTS}' AS float4[]), {vector}, 'word', 32),"
                        f" {heavy}({vector})"
                    )
                ).one()
                assert rank == pytest.approx(tenths / (tenths + 10), rel=1e-6), places
                band = next((name for least, name in bands if tenths >= least), None)
                assert listed == ([f"word {band}"] if band else []), (tenths, places, listed)


@pytest.mark.parametrize(
    "query", ["shall act", '"shall act"', "act or fund", "act -shall", "-shall", "fund -shall"]
)
def test_several_words_are_scored_at_every_match_and_never_from_the_heavy_words(varied, monkeypatch, query):
    # How near the words are counts towards the score, and the index does not know that.
    engine, _, _ = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", 10)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with engine.begin() as conn:
        event.listen(engine, "before_cursor_execute", capture)
        try:
            text_index.search(conn, query, text_index.Filters(), limit=3, offset=0)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert not [s for s in statements if "unit_heavy_words" in s]
        same_as_it_was(conn, query, text_index.Filters(), 3, 0, bound=10)


def test_any_term_search_gives_the_same_hits_when_many_units_match(varied, monkeypatch):
    engine, versions, works = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", 10)
    with engine.begin() as conn:
        for query in ("shall act", "commission", "shall"):
            for filters in filters_for(versions, works):
                same_as_it_was(conn, query, filters, 5, 0, any_terms=True, bound=10)


def test_any_term_search_gives_the_same_hits_in_the_same_order(varied):
    engine, versions, works = varied
    with engine.begin() as conn:
        for query in (
            "shall act",
            "commission minister shall",
            "shall",
            "the shall",
            "the",
            "qzxv",
            "what the act says",
        ):
            for filters in filters_for(versions, works):
                same_as_it_was(conn, query, filters, 25, 0, any_terms=True)
            for limit, offset in PAGES:
                same_as_it_was(conn, query, text_index.Filters(), limit, offset, any_terms=True)


def test_a_passage_set_aside_is_left_out_as_it_was(varied, monkeypatch):
    engine, _, _ = varied
    with engine.begin() as conn:
        [first] = text_index.search(conn, "shall", text_index.Filters(), limit=1, offset=0).rows
        exclude = (first.version_id, first.start_byte, first.end_byte)
        for bound in (text_index.COUNT_BOUND, 40):
            monkeypatch.setattr(text_index, "COUNT_BOUND", bound)
            for limit, offset in PAGES:
                found = same_as_it_was(
                    conn, "shall", text_index.Filters(), limit, offset, exclude=exclude, bound=bound
                )
                assert (first.version_id, first.start_byte) not in {
                    (r.version_id, r.start_byte) for r in found.rows
                }


def test_a_document_search_gives_the_same_documents_in_the_same_order(varied):
    engine, versions, works = varied
    with engine.begin() as conn:
        table = text_index._documents(conn)
        for query in ("shall", "commission", "shall act", '"shall act"', "act -shall", "the", "qzxv", "!!"):
            for filters in filters_for(versions, works):
                filters = replace(filters, kinds=())
                where, params = filters.where()
                then = conn.execute(
                    text(
                        f"SELECT {text_index._DOCUMENT_COLUMNS}, {THEN_SCORE} AS score FROM {table} u"
                        f" WHERE ({THEN_MATCH}) AND {where} ORDER BY score DESC, u.version_id LIMIT 7"
                    ),
                    params | {"q": query},
                ).all()
                found = text_index.search_documents(conn, query, filters, limit=7)
                assert found.state == "ok" and [dict(r._mapping) for r in found.rows] == [
                    dict(r._mapping) for r in then
                ], (query, filters)


def test_the_scope_of_a_search_is_what_counting_every_unit_finds(varied):
    engine, versions, works = varied
    with engine.begin() as conn:
        table, _ = text_index._tables(conn)
        documents = text_index._documents(conn)
        # A text with a document unit and no provisions is no text searched.
        conn.execute(
            text(
                f"INSERT INTO {documents} (version_id, work_id, start_byte, end_byte, language, config, jurisdiction_id,"
                " document_type, legal_status, content_hash, work_hash, names, document, context)"
                " VALUES ('v-empty', 'ng-empty', 0, 0, 'sw', 'simple', 'ng', 'act', 'current', :h, :h, '{}', '', '')"
            ),
            {"h": "c" * 64},
        )
        for filters in filters_for(versions, works):
            where, params = filters.where()
            then = conn.execute(
                text(
                    "SELECT count(DISTINCT u.version_id) AS texts, count(DISTINCT u.work_id) AS records,"
                    f" coalesce(array_agg(DISTINCT u.language), '{{}}') AS languages FROM {table} u WHERE {where}"
                ),
                params,
            ).one()
            scope = text_index.scope(conn, filters)
            assert (scope.texts, scope.records, scope.languages) == (
                then.texts, then.records, tuple(sorted(then.languages))
            ), filters  # fmt: skip
        assert text_index.scope(conn, text_index.Filters()).texts == len(versions)
    # A text's document unit is read, not its thousands of provisions, unless the filter is about those.
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "count(DISTINCT" in statement:
            statements.append(statement)

    with engine.begin() as conn:
        event.listen(engine, "before_cursor_execute", capture)
        try:
            text_index.scope(conn, text_index.Filters(jurisdictions=("ng",)))
            text_index.scope(conn, text_index.Filters(kinds=("section",)))
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert ['"document_unit" u' in s for s in statements] == [True, False]


def test_a_search_is_planned_for_its_values_and_sets_nothing_aside_for_others(varied):
    # psycopg prepares a statement on its sixth run, and PostgreSQL may keep a plan blind to its values after five
    # more. Each run must answer alike, and what it set aside for its own statements must end with them.
    engine, _, _ = varied
    settings = ("statement_timeout", "enable_seqscan", "work_mem", "plan_cache_mode")
    seen = {}

    def capture(c, cursor, statement, parameters, context, executemany):
        if "ORDER BY score DESC" in statement or "UNION ALL" in statement or "LIMIT 10001" in statement:
            with c.connection.cursor() as probe:
                for name in settings:
                    probe.execute(f"SELECT current_setting('{name}')")
                    seen[name] = probe.fetchone()[0]

    with engine.connect() as conn:
        for _ in range(13):
            with conn.begin():
                before = [conn.scalar(text(f"SELECT current_setting('{name}')")) for name in settings]
                seen.clear()
                event.listen(engine, "before_cursor_execute", capture)
                try:
                    found = text_index.search(conn, "shall", text_index.Filters(), limit=25, offset=0)
                finally:
                    event.remove(engine, "before_cursor_execute", capture)
                assert found.rows == same_as_it_was(conn, "shall", text_index.Filters(), 25, 0).rows
                assert found.rows and seen == {
                    "statement_timeout": "10s",
                    "enable_seqscan": "off",
                    "work_mem": text_index.SEARCH_MEMORY,
                    "plan_cache_mode": "force_custom_plan",
                }
                assert [conn.scalar(text(f"SELECT current_setting('{name}')")) for name in settings] == before


@pytest.mark.parametrize("bound", [None, 40])
def test_a_search_out_of_time_says_so_and_sets_nothing_aside(varied, monkeypatch, bound):
    engine, _, _ = varied
    monkeypatch.setattr(text_index, "TIMEOUT_MS", 20)
    if bound:
        monkeypatch.setattr(text_index, "COUNT_BOUND", bound)

    def slowly(conn, cursor, statement, parameters, context, executemany):
        if "ORDER BY score DESC" in statement:
            return "SELECT pg_sleep(5)", {}
        return statement, parameters

    settings = ("statement_timeout", "enable_seqscan", "work_mem", "plan_cache_mode")
    event.listen(engine, "before_cursor_execute", slowly, retval=True)
    try:
        with engine.begin() as conn:
            before = [conn.scalar(text(f"SELECT current_setting('{name}')")) for name in settings]
            assert (
                text_index.search(conn, "shall", text_index.Filters(), limit=5, offset=0).state == "timeout"
            )
            assert (
                text_index.search_documents(conn, "shall", text_index.Filters(), limit=5).state == "timeout"
            )
            assert [conn.scalar(text(f"SELECT current_setting('{name}')")) for name in settings] == before
    finally:
        event.remove(engine, "before_cursor_execute", slowly)


def test_a_database_before_the_index_of_heavy_words_scores_every_match(varied, monkeypatch):
    engine, _, _ = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", 40)
    with engine.begin() as conn:
        ns = namespace(conn)
        conn.execute(text(f"DROP INDEX {ns.qualified(conn, 'changes', 'text_unit_heavy_words')}"))
        conn.execute(text(f"DROP FUNCTION {ns.qualified(conn, 'changes', 'unit_heavy_words')}"))
        assert same_as_it_was(conn, "shall", text_index.Filters(), 5, 0, bound=40).rows


def test_a_search_for_one_word_reads_the_index_written_for_it(varied, monkeypatch):
    # The expression must be the index's to the letter, or each unit's vectors are read instead: the same
    # answers, and the same timeouts as before.
    engine, _, _ = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", 40)
    plans = []

    def explain(conn, cursor, statement, parameters, context, executemany):
        if "ORDER BY score DESC" in statement or " LIMIT 41" in statement:
            with conn.connection.cursor() as probe:
                probe.execute("EXPLAIN (COSTS OFF) " + statement, parameters)
                plans.append("\n".join(row[0] for row in probe.fetchall()))

    with engine.begin() as conn:
        event.listen(engine, "before_cursor_execute", explain)
        try:
            text_index.search(conn, "shall", text_index.Filters(), limit=5, offset=0)
            text_index.search(conn, "shall act", text_index.Filters(), limit=5, offset=0)
        finally:
            event.remove(engine, "before_cursor_execute", explain)
    assert any("Bitmap Index Scan on text_unit_heavy_words" in plan for plan in plans)
    assert all("Seq Scan" not in plan for plan in plans), plans


def test_the_roles_that_search_and_publish_can_use_the_index_of_heavy_words(varied, monkeypatch):
    engine, _, _ = varied
    monkeypatch.setattr(text_index, "COUNT_BOUND", 40)
    with engine.begin() as conn:
        ns = namespace(conn)
        roles = provision_roles(conn, ns)
        quote = conn.dialect.identifier_preparer.quote_identifier
        for role in roles.values():  # the disposable schema these tests run in stands for the service's own
            conn.execute(text(f"GRANT USAGE ON SCHEMA {quote(ns.base)} TO {quote(role)}"))
        expected = same_as_it_was(conn, "shall", text_index.Filters(), 5, 0, bound=40).rows
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL ROLE {quote(roles['serving'])}"))
            assert text_index.search(conn, "shall", text_index.Filters(), limit=5, offset=0).rows == expected
            assert text_index.scope(conn, text_index.Filters()).texts > 0
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL ROLE {quote(roles['release'])}"))
            table, _ = text_index._tables(conn)
            changed = conn.execute(
                text(
                    f"UPDATE {table} SET context = context || setweight(to_tsvector('simple', 'zebra zebra'), 'A')"
                    " WHERE version_id = :v AND start_byte = :s"
                ),
                {"v": expected[-1].version_id, "s": expected[-1].start_byte},
            )
            assert changed.rowcount == 1
            # Kept in the statement that changes the unit: the word of weight 2 is there to be read at once.
            arms, params = text_index._every_term({"'zebra'": list(text_index.ANALYSES)})
            with text_index._deadline(conn):
                [first] = text_index._heaviest(conn, table, arms, "true", params, 1)
            assert (first.version_id, first.start_byte) == (expected[-1].version_id, expected[-1].start_byte)
    finally:
        with engine.begin() as conn:
            for kind in ("policy", "staging", "corpus"):
                conn.execute(text(f"DROP SCHEMA {quote(ns.schema(kind))} CASCADE"))
            for role in roles.values():
                conn.execute(text(f"DROP OWNED BY {quote(role)}"))
                conn.execute(text(f"DROP ROLE {quote(role)}"))
