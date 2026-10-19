"""Who may download what (RSV-11, MAC-06; owner direction 2026-10-19): catalogue releases stay
keyless at /downloads; releases with full text download only from REST v1's /bulk, with a key whose
organisation's access includes bulk, for its plan's countries. The plans that include bulk (partner,
sovereign) are granted by agreement and never listed."""

import hashlib
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from accounts import keys
from accounts.store import TIERS, listed_plans
from api.credentials import CredentialGate, GateConfig
from api.site import create_downloads_app
from api.v1 import CLASSES, PUBLIC_PATHS, create_v1_app
from exports.release import write_release
from scripts.docs_reference import DATA

RESOURCE = "https://api.example.test/api/v1"
METADATA = "https://api.example.test/.well-known/oauth-protected-resource/api/v1"
PEPPER = b"b" * 32
REST = ["rest", "mcp"]
BULK = ["rest", "mcp", "bulk"]


def access(includes, scope=None):
    return {
        "ok": True,
        "organisation": "org_00000000000000000000000000",
        "credential": "key_" + "a" * 32,
        "tier": {"id": "sovereign", "includes": includes, "allowance": {"requests_per_minute": 60}},
        "scope": scope or {"all": True},
    }


def service(store, releases, check, usage=None):
    """/downloads and REST v1 behind the credential gate, as api.service mounts them."""
    parent = FastAPI()
    parent.mount("/downloads", create_downloads_app(store, releases))
    parent.mount(
        "/api/v1",
        CredentialGate(
            create_v1_app(store, releases, lambda: []),
            config=GateConfig(RESOURCE, "rest", "api", METADATA, public_paths=PUBLIC_PATHS, classes=CLASSES),
            check=check,
            record_usage=(usage.append if usage is not None else lambda entry: None),
            admission=None,
            verifier=None,
            key_pepper=PEPPER,
        ),
    )
    return TestClient(parent)


KEY = {"Authorization": f"Bearer {keys.generate().secret}"}


@pytest.fixture
def releases(cleared, store, tmp_path):
    store.save(cleared)
    write_release(cleared, tmp_path, "2026-10-05", "ng")  # corpus mode: holds the cleared text
    write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    return tmp_path


def test_catalogue_releases_download_with_no_key_and_are_listed_to_every_organisation(store, releases):
    client = service(store, releases, lambda asked: access(REST))
    metadata = client.get("/downloads/2026-10-05/ng/metadata/sources.jsonl")
    assert metadata.status_code == 200
    listed = client.get("/api/v1/releases", headers=KEY).json()
    assert [(r["mode"], r["download_base"]) for r in listed] == [
        ("metadata", "/downloads/2026-10-05/ng/metadata")
    ]


def test_a_full_text_release_is_refused_without_a_key_or_without_bulk_access(store, releases):
    client = service(store, releases, lambda asked: access(REST))
    keyless = client.get("/downloads/2026-10-05/ng/corpus/versions.jsonl")
    assert keyless.status_code == 404 and keyless.json()["code"] == "unavailable"
    assert "/api/v1/bulk" in keyless.json()["detail"]
    url = "/api/v1/bulk/2026-10-05/ng/corpus/versions.jsonl"
    missing = client.get(url)
    assert missing.status_code == 401 and missing.json()["code"] == "credential_required"
    refused = client.get(url, headers=KEY)
    assert refused.status_code == 403 and refused.json()["code"] == "entitlement_required"
    assert refused.headers["content-type"] == "application/problem+json"
    assert "bulk releases" in refused.json()["detail"] and "www-authenticate" not in refused.headers
    # Without the gate's word for it, the route itself refuses too.
    bare = TestClient(create_v1_app(store, releases, lambda: []))
    assert bare.get("/bulk/2026-10-05/ng/corpus/versions.jsonl").json()["code"] == "entitlement_required"
    assert [r["mode"] for r in bare.get("/releases").json()] == ["metadata"]


def test_an_organisation_with_bulk_access_lists_and_downloads_full_text_releases(store, releases):
    usage = []
    client = service(store, releases, lambda asked: access(BULK), usage)
    listed = {r["mode"]: r for r in client.get("/api/v1/releases", headers=KEY).json()}
    assert listed["metadata"]["download_base"] == "/downloads/2026-10-05/ng/metadata"
    corpus = listed["corpus"]
    assert corpus["download_base"] == "/api/v1/bulk/2026-10-05/ng/corpus"
    assert corpus["files"]["versions.jsonl"]["records"] == 1
    sent = 0
    for name, details in corpus["files"].items():
        response = client.get(f"{corpus['download_base']}/{name}", headers=KEY)
        assert response.status_code == 200, name
        assert hashlib.sha256(response.content).hexdigest() == details["sha256"], name
        sent += len(response.content)
    # Counted as bulk use, every byte.
    assert {entry["class"] for entry in usage} == {"rest", "bulk"}
    assert sum(entry["bytes"] for entry in usage if entry["class"] == "bulk") == sent > 0
    # Still never served keyless.
    assert client.get("/downloads/2026-10-05/ng/corpus/versions.jsonl").status_code == 404


def test_bulk_text_is_only_for_the_countries_of_the_plan(store, releases):
    client = service(store, releases, lambda asked: access(BULK, {"jurisdictions": ["rw"]}))
    assert [r["mode"] for r in client.get("/api/v1/releases", headers=KEY).json()] == ["metadata"]
    refused = client.get("/api/v1/bulk/2026-10-05/ng/corpus/versions.jsonl", headers=KEY)
    assert refused.status_code == 403 and refused.json()["code"] == "jurisdiction_not_included"


def test_a_withdrawn_or_unknown_bulk_file_is_a_problem_document(cleared, store, releases):
    client = service(store, releases, lambda asked: access(BULK))
    unknown = client.get("/api/v1/bulk/2026-10-05/ng/corpus/secrets.txt", headers=KEY)
    assert unknown.status_code == 404 and unknown.json()["code"] == "unavailable"
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    withdrawn = client.get("/api/v1/bulk/2026-10-05/ng/corpus/versions.jsonl", headers=KEY)
    assert withdrawn.status_code == 410 and withdrawn.json()["code"] == "withdrawn"
    assert withdrawn.headers["content-type"] == "application/problem+json"


def test_bulk_access_withdrawn_during_a_download_stops_it(store, releases):
    answers = iter([access(BULK), access(REST)])
    client = service(store, releases, lambda asked: next(answers))
    refused = client.get("/api/v1/bulk/2026-10-05/ng/corpus/versions.jsonl", headers=KEY)
    assert refused.status_code == 403 and refused.json()["code"] == "entitlement_required"
    assert b"Synthetic fixture" not in refused.content


def test_plans_granted_by_agreement_are_never_listed():
    tiers = {t["id"]: t for t in json.loads(TIERS.read_text())}
    listed = {t["id"] for t in listed_plans()}
    assert listed == {"starter", "developer", "team", "enterprise", "public-interest"}
    for name in ("pilot", "partner", "sovereign"):
        assert tiers[name]["published"] is False and "action" not in tiers[name]["listing"], name
    # The documentation's plans table is built from the listed plans only.
    assert {t["id"] for t in json.loads((DATA / "plans.json").read_text())} == listed
    # Only the plans granted by agreement include bulk, and they are proposals until D31.
    assert {t["id"] for t in tiers.values() if "bulk" in t["includes"]} == {"partner", "sovereign"}
    for name in ("partner", "sovereign"):
        assert "pending decision D31" in tiers[name]["description"]
    partner = tiers["partner"]["allowance"]
    assert (partner["requests_per_minute"], partner["burst"], partner["concurrency"]) == (6000, 3000, 100)
    assert partner["bytes_per_day"] == 50_000_000_000
    team, sovereign = tiers["team"]["allowance"], tiers["sovereign"]["allowance"]
    assert {k: sovereign[k] for k in ("requests_per_minute", "burst", "concurrency", "bytes_per_day")} == {
        k: team[k] for k in ("requests_per_minute", "burst", "concurrency", "bytes_per_day")
    }


def test_every_plan_fits_the_tier_table():
    # The accounts schema's constraints (rights/sql/v0021/accounts.sql), so seeding never fails.
    for tier in json.loads(TIERS.read_text()):
        assert re.fullmatch(r"[a-z][a-z0-9-]{1,31}", tier["id"]), tier["id"]
        assert 1 <= len(tier["name"]) <= 80 and 1 <= len(tier["description"]) <= 500, tier["id"]
        assert tier["includes"] and set(tier["includes"]) <= {"rest", "mcp", "bulk"}, tier["id"]
        assert isinstance(tier["allowance"], dict) and isinstance(tier["published"], bool), tier["id"]
