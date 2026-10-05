from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from api.main import create_app
from api.store import Store
from scripts.check_launch import launch_errors


def test_health_is_ready_only_after_migration(store, tmp_path):
    assert TestClient(create_app(store, tmp_path)).get("/healthz").json() == {"status": "ok"}
    empty = Store(create_engine(f"sqlite:///{tmp_path / 'unmigrated.db'}"))
    response = TestClient(create_app(empty, tmp_path)).get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "sqlite" not in response.text


def test_public_identity_is_explicit_and_contains_no_secrets(store, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_REPOSITORY_URL", "https://github.com/example/open-alvary")
    monkeypatch.setenv("PUBLIC_MAINTAINER", "Test maintainer")
    monkeypatch.setenv("PUBLIC_CONTACT_EMAIL", "maintainer@example.org")
    monkeypatch.setenv("POSTGRES_PASSWORD", "never-publish-this-secret")
    response = TestClient(create_app(store, tmp_path)).get("/project")
    assert response.json()["maintainer"] == "Test maintainer"
    assert "never-publish" not in response.text


def test_launch_blocks_unknown_owner_and_licence():
    errors = launch_errors({}, "Status: pending owner approval")
    assert len(errors) == 6


def test_launch_requires_both_config_and_approved_licence():
    env = {
        "PUBLIC_REPOSITORY_URL": "https://github.com/example/open-alvary",
        "PUBLIC_MAINTAINER": "Test maintainer",
        "PUBLIC_CONTACT_EMAIL": "test@example.org",
        "PUBLIC_DOMAIN": "open.example.org",
        "POSTGRES_PASSWORD": "a" * 64,
        "METADATA_LICENCE": "CC-BY-4.0",
    }
    assert launch_errors(env, "Status: pending owner approval")
    assert launch_errors(env, "Status: approved") == []
