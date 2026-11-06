"""The public service on two database logins (api.service, rights.logins): every route that serves the catalogue
reads through the catalogue's store and touches no other, and the two pools must fit the database's budget
together. tests/test_postgres_logins.py runs both as the logins themselves, with admission and keys."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from api.service import create_service
from api.store import Store, metadata
from exports.release import write_release
from tests.test_admission import config

SOURCE = "ng-foi-act-2011"


def recorded(engine) -> list[str]:
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, *rest: statements.append(sql))
    return statements


@pytest.fixture
def stores(catalogue):
    """The console's database, which holds no catalogue at all, and the catalogue's."""
    engines = [
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        for _ in range(2)
    ]
    metadata.create_all(engines[1])
    console, reader = Store(engines[0]), Store(engines[1])
    reader.save(catalogue)
    yield console, reader
    for engine in engines:
        engine.dispose()


def test_every_route_that_serves_the_catalogue_reads_the_catalogues_store_and_no_other(
    stores, catalogue, tmp_path
):
    console, reader = stores
    write_release(catalogue, tmp_path, "2026-10-05", "ng", metadata_only=True)
    asked = recorded(console.engine)
    read = recorded(reader.engine)
    client = TestClient(create_service(console, tmp_path, catalogue=reader))
    for path in (
        "/healthz",
        "/site/v1/coverage",
        "/site/v1/sources",
        f"/site/v1/sources/{SOURCE}",
        f"/site/v1/sources/{SOURCE}/versions",
        f"/site/v1/sources/{SOURCE}/relationships",
        f"/site/v1/rights/{SOURCE}",
        "/site/v1/search?q=freedom",
        "/site/v1/releases",
        "/downloads/2026-10-05/ng/metadata/sources.jsonl",
        "/sitemap.xml",
        "/api/sources",
        f"/api/sources/{SOURCE}",
        "/api/releases",
    ):
        assert client.get(path).status_code == 200, path
    assert read and asked == []


def test_without_a_catalogue_store_the_one_store_serves_everything(stores, catalogue, tmp_path):
    _, reader = stores
    client = TestClient(create_service(reader, tmp_path))
    assert client.get(f"/site/v1/sources/{SOURCE}").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def admission_files(folder, monkeypatch, api: dict, site: dict) -> None:
    for variable, name, overrides in (
        ("ALVARY_ADMISSION_CONFIG", "api", api),
        ("ALVARY_SITE_ADMISSION_CONFIG", "site", site),
    ):
        values = {
            k: (v.hex() if isinstance(v, bytes) else v)
            for k, v in config(**overrides).__dict__.items()
            if v is not None
        }
        path = folder / f"{name}.json"
        path.write_text(json.dumps(values))
        monkeypatch.setenv(variable, str(path))


def test_two_logins_are_two_pools_and_together_they_must_fit_the_database_budget(monkeypatch, tmp_path):
    """The example budget leaves 15 connections (20 less a reserve of 3 and 2 for other workers): 10 and 10 is too
    many for two pools, though either alone fits, as one pool did."""
    monkeypatch.setenv("ALVARY_CATALOGUE_DATABASE_URL", f"sqlite:///{tmp_path / 'catalogue.db'}")
    admission_files(tmp_path, monkeypatch, {"public_pool_size": 10}, {"public_pool_size": 10})
    with pytest.raises(ValueError, match="together exceed the database budget"):
        create_service(releases=tmp_path)
    # Within it, the service goes on to build, and only stops at admission needing PostgreSQL.
    admission_files(tmp_path, monkeypatch, {"public_pool_size": 6}, {"public_pool_size": 8})
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        create_service(releases=tmp_path)


def test_two_logins_need_both_admission_classes_to_size_their_pools(monkeypatch, tmp_path):
    monkeypatch.setenv("ALVARY_CATALOGUE_DATABASE_URL", f"sqlite:///{tmp_path / 'catalogue.db'}")
    admission_files(tmp_path, monkeypatch, {}, {})
    monkeypatch.delenv("ALVARY_ADMISSION_CONFIG")
    with pytest.raises(ValueError, match="both admission classes"):
        create_service(releases=tmp_path)
