import hashlib

import pytest
from fastapi.testclient import TestClient

from api.vercel import create_vercel_app
from scripts import build_vercel


def test_vercel_snapshot_serves_prefixed_api_and_verified_downloads(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Open Alvary</h1>")
    client = TestClient(create_vercel_app(frontend=tmp_path))
    assert client.get("/").text == "<h1>Open Alvary</h1>"
    assert client.get("/api/healthz").json() == {"status": "ok"}
    assert client.get("/api/coverage").json()["catalogued_sources"] == 4
    assert client.get("/api/coverage").json()["open_full_text_sources"] == 0
    result = client.get("/api/search?q=freedom").json()
    assert result["total"] == 1
    source_id = result["items"][0]["id"]
    assert client.get(f"/api/sources/{source_id}/versions").json()["items"] == []
    assert client.post("/api/sources", json={}).status_code == 405
    assert client.get("/sources").status_code == 404
    releases = client.get("/api/releases").json()
    assert releases
    for release in releases:
        for filename, info in release["files"].items():
            response = client.get("/api" + release["download_base"] + "/" + filename)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert hashlib.sha256(response.content).hexdigest() == info["sha256"]


def test_vercel_snapshot_rejects_even_cleared_full_text(cleared):
    with pytest.raises(ValueError, match="metadata only"):
        create_vercel_app(cleared)


def test_vercel_production_build_requires_publication_configuration(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("METADATA_LICENCE", "pending")
    with pytest.raises(SystemExit, match="Public launch blocked"):
        build_vercel.main()
