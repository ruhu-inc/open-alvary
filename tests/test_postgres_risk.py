"""Actual native SQL compilation, quoting, isolation, and diagnostic preservation."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from rights.database import PolicyNamespace
from rights.sql_resources import context, render

pytestmark = pytest.mark.postgres
GOLDEN = json.loads(Path("tests/fixtures/oa-text-1-golden.json").read_text())


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["name"])
def test_native_serializer_matches_frozen_hand_calculated_vectors(pg_engine, case):
    migrate(pg_engine)
    with pg_engine.begin() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        fn = ns.qualified(conn, "policy", "oa_compile")
        data = {"source": {c["id"]: c for c in case["candidates"]}, "order": case["order"]}
        result = conn.scalar(
            text(f"SELECT {fn}(CAST(:source AS jsonb), CAST(:plan AS jsonb))"),
            {"plan": json.dumps(case["plan"]), "source": json.dumps(data)},
        )
        # Expected text is a frozen constant independent of either implementation.
        assert result["text"].encode() == case["expected_utf8"].encode()


def test_postgresql_literal_and_identifier_quoting_roundtrip_unusual_names(pg_engine):
    with pg_engine.begin() as conn:
        ns = PolicyNamespace("test_quote'\\\"é")
        tokens = context(conn, ns, {"role_literal_test": "_unusual'\\value"})
        values = conn.execute(
            text(render("SELECT {{base_literal}} AS {{base}}, {{role_literal_test}}", tokens))
        ).one()
        assert values[0] == ns.base and values[1].endswith("_unusual'\\value")


def test_sql_revision_refuses_unknown_body_edit_before_any_replacement(pg_engine):
    from alembic import command
    from alembic.config import Config

    with pg_engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0010")
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        conn.rollback()
        tx = conn.begin()
        fn = ns.qualified(conn, "policy", "current_actor")
        conn.exec_driver_sql(
            f"CREATE OR REPLACE FUNCTION {fn}() RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$ SELECT NULL::uuid $$"
        )
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="boundary review"):
            command.upgrade(cfg, "0011")
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0010"
        tx.rollback()
    migrate(pg_engine)


@pytest.mark.parametrize(
    "title,query",
    [
        ("Straße", "STRASSE"),
        ("ＦＯＯ  bar", "foo bar"),
        ("Café", "Cafe\u0301"),
        ("100%_literal", "%_"),
        ("No\t\n amount", "no amount"),
    ],
)
def test_postgresql_metadata_search_keeps_exact_unicode_semantics(pg_engine, catalogue, title, query):
    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository
    from api.store import Store

    catalogue.sources[0].title = title
    Store(pg_engine).save(catalogue)
    with pg_engine.begin() as conn:
        page = LegacyCatalogue(LegacyRepository(conn)).search(query)
        assert page.total == 1 and page.items[0].id == catalogue.sources[0].id


def test_indexed_10000_metadata_workload_decodes_only_the_page(pg_engine, catalogue, monkeypatch):
    from time import perf_counter

    from test_catalogue_remediation import expanded

    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository
    from api.store import Store

    Store(pg_engine).save(expanded(catalogue, 10000))
    decoded = []
    original = LegacyRepository._pair

    def pair(row):
        decoded.append(row.source["id"])
        return original(row)

    monkeypatch.setattr(LegacyRepository, "_pair", staticmethod(pair))
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("ANALYZE sources")
        conn.exec_driver_sql("ANALYZE rights")
        repo = LegacyRepository(conn)
        start = perf_counter()
        page = LegacyCatalogue(repo).search("cataloguebatch", offset=9975, limit=25)
        elapsed = perf_counter() - start
        assert page.total == 10000 and [s.id for s in page.items] == [
            f"fixture-{i:05d}" for i in range(9975, 10000)
        ]
        assert len(decoded) == 25
        # Verify the real planner can use the exact expression index, not a different test query.
        confirmed, _ = repo.search_predicates("cataloguebatch 09999")
        statement = repo._select_sources().where(confirmed)
        conn.exec_driver_sql("SET LOCAL enable_seqscan=off")
        compiled = statement.compile(conn)
        plan = conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + str(compiled), compiled.params).scalar_one()
        assert "ix_sources_metadata_trigram" in json.dumps(plan)
        print(f"S1 synthetic 10000-metadata page: {elapsed:.4f}s; decoded25; exact expression index verified")


def test_empty_search_rollback_removes_only_owned_extension_and_preserves_legacy_data(pg_engine, catalogue):
    from alembic import command
    from alembic.config import Config

    from api.store import Store

    store = Store(pg_engine)
    store.save(catalogue)
    before = store.load().model_dump(mode="json")
    with pg_engine.begin() as conn:
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0011")
        assert not conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm')"))
    assert store.load().model_dump(mode="json") == before
    migrate(pg_engine)
    assert store.load().model_dump(mode="json") == before
