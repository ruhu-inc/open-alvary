import pytest

from api.store import engine_for


@pytest.mark.parametrize("url", [None, "", "sqlite:///secret.db", "invalid-password-url"])
def test_persistent_mode_rejects_missing_or_non_postgres_url(monkeypatch, url):
    monkeypatch.setenv("ALVARY_RUNTIME_MODE", "persistent")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="Persistent mode") as error:
        engine_for(url)
    assert "secret.db" not in str(error.value)
    assert "invalid-password" not in str(error.value)


def test_persistent_mode_uses_explicit_postgres_without_connecting(monkeypatch):
    monkeypatch.setenv("ALVARY_RUNTIME_MODE", "persistent")
    engine = engine_for("postgresql+psycopg://test:test@localhost/open_alvary_test")
    assert engine.dialect.name == "postgresql"
    engine.dispose()


def test_unknown_runtime_mode_fails(monkeypatch):
    monkeypatch.setenv("ALVARY_RUNTIME_MODE", "persistant")
    with pytest.raises(ValueError, match="ALVARY_RUNTIME_MODE"):
        engine_for("sqlite://")
