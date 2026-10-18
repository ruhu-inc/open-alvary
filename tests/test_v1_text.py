"""The keyed API rechecks a record's texts immediately before sending them (API-02)."""

from fastapi.testclient import TestClient

from api.catalogue import LegacyCatalogue, SourceUnavailable
from api.v1 import create_v1_app


def test_texts_are_rechecked_immediately_before_they_are_sent(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    source = cleared.sources[0].id
    sent = client.get(f"/sources/{source}/versions")
    assert sent.status_code == 200 and sent.json()["items"][0]["canonical_text"].startswith("Synthetic")
    assert sent.headers["cache-control"] == "no-store"
    read = LegacyCatalogue.versions
    calls = []

    def withdrawn_after_reading(self, source_id):
        calls.append(source_id)
        if len(calls) > 1:  # the recheck: the record stopped being public after it was read
            raise SourceUnavailable(source_id)
        return read(self, source_id)

    monkeypatch.setattr(LegacyCatalogue, "versions", withdrawn_after_reading)
    refused = client.get(f"/sources/{source}/versions")
    assert calls == [source, source]
    assert refused.status_code == 503 and refused.headers["content-type"] == "application/problem+json"
    assert refused.json()["code"] == "temporarily_unavailable" and refused.json()["retryable"] is True
    assert "Synthetic" not in refused.text and refused.headers["cache-control"] == "no-store"
