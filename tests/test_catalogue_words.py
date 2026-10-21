"""G11: no public route or tool finds records by part of a word or scans text for a substring. Every
search entry point (/search, the website's /site/v1/search, MCP search_catalogue) matches whole words of
a record's title, citation and summary, and published texts only through the section index; on
PostgreSQL the words come from an index (tests/test_postgres_search.py). Checked by behaviour, by the
SQL each search sends, and by reading the serving code."""

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from api.catalogue import NO_WORDS
from api.main import create_app
from api.repositories import LegacyRepository
from api.site import create_site_app
from api.v1 import create_v1_app

ROOT = Path(__file__).resolve().parents[1]
# The code that serves the public website, the API and MCP.
SERVING = sorted([*ROOT.glob("api/*.py"), *ROOT.glob("retrieval/*.py")])
# SQL that compares by substring: a LIKE pattern opening with a wildcard, or a search for a position.
SUBSTRING_SQL = re.compile(
    r"(?i)\b(i?like|similar\s+to)\s*'%|'%'\s*\|\||\|\|\s*'%'|\bstrpos\s*\(|\bposition\s*\("
)
# Named and explained: text_index.named_works asks whether a whole name (a title, citation or alias,
# folded, between spaces) occurs in the query, to recognise an instrument the query names (RSV-03); it
# never looks for the query inside a title or a text.
ALLOWED = {("api/text_index.py", "named_works")}
TEXT_FIELDS = re.compile(r"canonical_text|\btitle\b|\bcitation\b|\bsummary\b|normalize_citation")


def substring_scans(source: str, relative: str) -> list[str]:
    """Where a module compares by substring: in Python (``x in <a text>``, ``<a text>.find(x)``), in a
    SQLAlchemy LIKE that opens with a wildcard or a ``contains``, or in SQL it writes."""
    tree = ast.parse(source)
    owners = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                owners.setdefault(id(node), function.name)
    found = []
    for node in ast.walk(tree):
        if (relative, owners.get(id(node))) in ALLOWED:
            continue
        where = f"{relative}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=True):
                literal = isinstance(
                    right, ast.Tuple | ast.List | ast.Set | ast.Dict | ast.SetComp | ast.ListComp
                )
                if (
                    isinstance(op, ast.In | ast.NotIn)
                    and not literal
                    and TEXT_FIELDS.search(ast.unparse(right))
                ):
                    found.append(f"{where}: {ast.unparse(node)}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method, target = node.func.attr, ast.unparse(node.func.value)
            arguments = " ".join(ast.unparse(a) for a in node.args)
            if method in {"find", "rfind", "index", "count"} and "canonical_text" in target:
                found.append(f"{where}: {ast.unparse(node)}")
            if method in {"contains", "icontains", "regexp_match"}:
                found.append(f"{where}: {ast.unparse(node)}")
            if method in {"like", "ilike", "not_like", "not_ilike"} and re.match(
                r"""^["']%|literal_pattern""", arguments
            ):
                found.append(f"{where}: {ast.unparse(node)}")
        elif isinstance(node, ast.Name) and node.id == "literal_pattern":
            found.append(f"{where}: literal_pattern")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SUBSTRING_SQL.search(node.value)
        ):
            found.append(f"{where}: {node.value[:80]!r}")
    return found


def test_no_serving_code_compares_text_by_substring():
    assert SERVING and not [
        scan
        for path in SERVING
        for scan in substring_scans(path.read_text(), path.relative_to(ROOT).as_posix())
    ]


def test_the_check_finds_the_substring_scans_it_was_written_for():
    sample = (
        "def search(term, source, version, table):\n"
        "    a = term in normalize_citation(source.title)\n"
        "    b = any(term in v.canonical_text.casefold() for v in version)\n"
        "    c = table.c.title.like('%' + term + '%')\n"
        "    d = table.c.title.contains(term)\n"
        "    e = \"SELECT id FROM sources WHERE title ILIKE '%' || :q || '%'\"\n"
        "    f = version.canonical_text.find(term)\n"
        "    g = term in (source.title, source.citation)\n"  # exact: a value among values
    )
    assert len(substring_scans(sample, "api/sample.py")) == 6


@pytest.fixture
def apps(cleared, store, tmp_path):
    store.save(cleared)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(store.engine, "before_cursor_execute", capture)
    yield (
        TestClient(create_app(store, tmp_path)),
        TestClient(create_site_app(store, tmp_path)),
        TestClient(create_v1_app(store, tmp_path, lambda: [])),
        statements,
    )
    event.remove(store.engine, "before_cursor_execute", capture)


def every_search(apps, q):
    """The ids each search entry point finds for ``q``, and their answers."""
    legacy, site, v1, _ = apps
    answers = {
        "/search": legacy.get("/search", params={"q": q}).json(),
        "/api/v1/search": v1.get("/search", params={"q": q}).json(),
        "/site/v1/search": site.get("/search", params={"q": q}).json(),  # mounted at /site/v1
    }
    call = {"name": "search_catalogue", "arguments": {"query": q, "limit": 25}}
    reply = legacy.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
    ).json()
    answers["search_catalogue"] = json.loads(reply["result"]["content"][0]["text"])
    return {name: {s["id"] for s in answer["items"]} for name, answer in answers.items()}, answers


@pytest.mark.parametrize(
    "q,expected",
    [
        ("investments", {"ng-isa-2007", "ng-isa-2025"}),
        ("invest", set()),  # never part of a word
        ("SECURITIES  investments", {"ng-isa-2007", "ng-isa-2025"}),  # any order, any case
        ("CBN", {"ng-cbn-act-2007"}),  # a citation
        ("fundamental rights", {"ng-constitution-1999"}),  # a summary
        ("information freedom 2011", {"ng-foi-act-2011"}),
        ("freedom 2007", set()),  # every word, in one record
    ],
)
def test_every_entry_point_finds_records_by_whole_words(apps, q, expected):
    found, _ = every_search(apps, q)
    assert found == dict.fromkeys(found, expected)


def test_published_texts_are_never_scanned_for_words_and_no_sql_compares_by_substring(apps, monkeypatch):
    class Tripwire(str):
        def _scanned(self, *args, **kwargs):
            raise AssertionError("A search scanned a published text")

        __contains__ = find = rfind = index = count = casefold = lower = upper = split = _scanned

    original = LegacyRepository.version

    def version(self, version_id, source_id):
        loaded = original(self, version_id, source_id)
        return loaded.model_copy(update={"canonical_text": Tripwire(loaded.canonical_text)})

    monkeypatch.setattr(LegacyRepository, "version", version)
    # The cleared fixture's text says "Synthetic fixture — not legislation"; its record says neither.
    found, answers = every_search(apps, "legislation")
    assert found == dict.fromkeys(found, set())
    assert all("notices" not in answer for answer in answers.values())  # SQLite: the metadata-only pilot
    found, _ = every_search(apps, "constitution")
    assert found == dict.fromkeys(found, {"ng-constitution-1999"})
    statements = apps[3]
    assert statements and not [s for s, _ in statements if re.search(r"(?i)\b(i?like|glob|instr)\b", s)]
    assert not [p for _, p in statements if any(isinstance(v, str) and v.startswith("%") for v in p)]


def test_a_search_with_no_words_finds_nothing_and_says_why(apps):
    legacy, site, v1, _ = apps
    for answer in (
        legacy.get("/search", params={"q": "%_"}).json(),
        v1.get("/search", params={"q": "%"}).json(),
        site.get("/search", params={"q": "--"}).json(),
    ):
        assert answer["total"] == 0 and answer["items"] == [] and answer["notices"] == [NO_WORDS]
    assert legacy.get("/search", params={"q": "  "}).status_code == 422  # blank is still refused
