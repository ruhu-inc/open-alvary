"""Final policy check immediately before emission, and byte charging order."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.catalogue import LegacyCatalogue
from api.emission import CheckedResponse
from api.main import create_app
from exports.release import write_release
from rights.bytes import LedgerConflict, Reservation, Scope

TEXT = "Section 1. Test only."


def withdraw_after_lookup(monkeypatch, store, corpus):
    """Commit a rights withdrawal between the handler's lookup and the emission check."""
    original = LegacyCatalogue.versions
    calls = []

    def racing(self, source_id):
        page = original(self, source_id)
        if not calls:
            calls.append(source_id)
            corpus.rights[0].status = "RIGHTS_UNDER_REVIEW"
            store.save(corpus)
        return page

    monkeypatch.setattr(LegacyCatalogue, "versions", racing)


def test_text_is_served_when_still_permitted(cleared, store, tmp_path):
    store.save(cleared)
    response = TestClient(create_app(store, tmp_path)).get(f"/sources/{cleared.sources[0].id}/versions")
    assert response.status_code == 200 and TEXT in response.text


def test_withdrawal_after_lookup_and_before_send_emits_no_text(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    withdraw_after_lookup(monkeypatch, store, cleared)
    response = TestClient(create_app(store, tmp_path)).get(f"/sources/{cleared.sources[0].id}/versions")
    assert response.status_code == 409 and TEXT not in response.text


def test_policy_read_failure_at_emission_fails_closed(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    original = LegacyCatalogue.versions
    calls = []

    def failing_recheck(self, source_id):
        calls.append(source_id)
        if len(calls) > 1:
            raise RuntimeError("policy store unavailable")
        return original(self, source_id)

    monkeypatch.setattr(LegacyCatalogue, "versions", failing_recheck)
    response = TestClient(create_app(store, tmp_path)).get(f"/sources/{cleared.sources[0].id}/versions")
    assert response.status_code == 503 and TEXT not in response.text


def test_release_withdrawn_between_lookup_and_stream_is_not_sent(catalogue, store, tmp_path, monkeypatch):
    from api import release_adapter

    store.save(catalogue)
    write_release(catalogue, tmp_path, "2026-10-07", "ng", True)
    original = release_adapter.LegacyReleases.file
    calls = []

    def racing(self, *args):
        path = original(self, *args)
        if not calls:
            calls.append(args)
            catalogue.rights[0].metadata_public = False
            store.save(catalogue)
        return path

    monkeypatch.setattr(release_adapter.LegacyReleases, "file", racing)
    response = TestClient(create_app(store, tmp_path)).get("/releases/2026-10-07/ng/metadata/sources.jsonl")
    assert response.status_code == 410 and "ng-" not in response.text


class FakeLedger:
    def __init__(self, reserve=True, emit=True):
        self.reserve_ok, self.emit_ok = reserve, emit
        self.calls = []

    def reserve(self, admission_id, nbytes, scopes, lease):
        self.calls.append(("reserve", nbytes))
        if self.reserve_ok:
            return Reservation(True, uuid4())
        return Reservation(False, blocked=("ip:a",), retry_after=9)

    def begin_emission(self, reservation_id):
        self.calls.append(("begin",))
        return self.emit_ok

    def release(self, reservation_id):
        self.calls.append(("release",))

    def settle(self, reservation_id, sent, receipt):
        self.calls.append(("settle", sent))
        if sent < 0:
            raise LedgerConflict("x")


def app_for(response):
    from fastapi import FastAPI

    app = FastAPI()
    app.get("/t")(lambda: response)
    return TestClient(app)


SCOPES = [Scope("ip:a", 10 * 1024 * 1024, 60)]


def test_charged_emission_reserves_rechecks_begins_sends_and_settles():
    ledger = FakeLedger()
    order = []
    response = CheckedResponse(
        b'{"text":"x"}',
        lambda: order.append("recheck") or True,
        ledger=ledger,
        scopes=SCOPES,
        lease_seconds=60,
        charged=True,
    )
    assert app_for(response).get("/t").status_code == 200
    assert [c[0] for c in ledger.calls] == ["reserve", "begin", "settle"] and order == ["recheck"]
    assert ledger.calls[-1] == ("settle", len(b'{"text":"x"}'))


def test_denied_recheck_releases_the_unsent_reservation():
    ledger = FakeLedger()
    response = CheckedResponse(
        b"{}", lambda: False, ledger=ledger, scopes=SCOPES, lease_seconds=60, charged=True
    )
    assert app_for(response).get("/t").status_code == 409
    assert [c[0] for c in ledger.calls] == ["reserve", "release"]


def test_identity_byte_exhaustion_is_429_without_body():
    ledger = FakeLedger(reserve=False)
    response = CheckedResponse(
        b'{"text":"x"}', lambda: True, ledger=ledger, scopes=SCOPES, lease_seconds=60, charged=True
    )
    result = app_for(response).get("/t")
    assert result.status_code == 429 and result.headers["Retry-After"] == "9" and "text" not in result.text


def test_missing_byte_limits_block_charged_text():
    response = CheckedResponse(b'{"text":"x"}', lambda: True, charged=True)
    result = app_for(response).get("/t")
    assert result.status_code == 503 and "text" not in result.text


def test_lost_delivery_lease_sends_nothing():
    ledger = FakeLedger(emit=False)
    response = CheckedResponse(
        b'{"text":"x"}', lambda: True, ledger=ledger, scopes=SCOPES, lease_seconds=60, charged=True
    )
    assert app_for(response).get("/t").status_code == 503
    assert "settle" not in [c[0] for c in ledger.calls]


def test_oversized_buffer_is_refused_at_construction():
    with pytest.raises(ValueError):
        CheckedResponse(b"x" * (10 * 1024 * 1024 + 1), lambda: True)
