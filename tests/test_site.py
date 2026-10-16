"""MAC-01/02 activation A: the website's own data path, public downloads and the legacy API beside them."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.admission import GLOBAL_KEY, AdmissionMiddleware, pseudonym
from api.service import create_service
from exports.release import write_release
from tests.test_admission import SECRET, FakeStore, config

SOURCE = "ng-foi-act-2011"


@pytest.fixture
def client(catalogue, store, tmp_path):
    store.save(catalogue)
    write_release(catalogue, tmp_path, "2026-10-05", "ng")
    return TestClient(create_service(store, tmp_path))


def test_every_page_of_the_website_has_its_route(client):
    for route in [
        "/site/v1/project",
        "/site/v1/coverage",
        "/site/v1/sources",
        f"/site/v1/sources/{SOURCE}",
        f"/site/v1/sources/{SOURCE}/versions",
        f"/site/v1/rights/{SOURCE}",
        "/site/v1/releases",
        "/site/v1/search?q=freedom",
    ]:
        response = client.get(route)
        assert response.status_code == 200, route
        assert response.headers["cache-control"] == "no-store", route
    assert client.get("/site/v1/search?q=freedom").json()["total"] == 1


def test_fixed_pages_of_twenty_up_to_page_25(client):
    first = client.get("/site/v1/sources").json()
    assert first["page"] == 1 and first["page_size"] == 20 and first["last_page"] == 1
    assert client.get("/site/v1/sources?page=25").status_code == 200
    beyond = client.get("/site/v1/sources?page=26")
    assert beyond.status_code == 400 and beyond.json()["code"] == "beyond_bounds"
    assert "refine" in beyond.json()["detail"]
    assert client.get("/site/v1/search?q=freedom&page=26").json()["code"] == "beyond_bounds"
    for bad in ["page=0", "page=-1", "page=two", "limit=100", "offset=20", "cursor=x", "access=secret"]:
        response = client.get("/site/v1/sources?" + bad)
        assert response.status_code == 400 and response.json()["code"] == "invalid_input", bad


def test_search_needs_a_bounded_query(client):
    assert client.get("/site/v1/search").json()["code"] == "invalid_input"
    assert client.get("/site/v1/search?q=" + "x" * 201).json()["code"] == "invalid_input"
    assert client.get("/site/v1/search?q=%20%20").status_code == 400


def test_credentials_are_refused_and_never_served(client):
    for header in ["Bearer oa_live_x", "Basic dXNlcjpwYXNz", "anything"]:
        response = client.get("/site/v1/sources", headers={"Authorization": header})
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/problem+json"
        body = response.json()
        assert body["code"] == "credential_not_accepted" and body["request_id"]
        assert response.headers["cache-control"] == "no-store"


def test_no_cors_and_no_writes(client):
    response = client.get("/site/v1/sources", headers={"Origin": "https://elsewhere.example"})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options(
        "/site/v1/sources",
        headers={"Origin": "https://elsewhere.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in preflight.headers
    assert client.post("/site/v1/sources", json={}).status_code == 405


def test_missing_source_is_a_problem_not_a_page(client):
    response = client.get("/site/v1/sources/missing")
    assert response.status_code == 404 and response.json()["code"] == "unavailable"
    assert response.headers["content-type"] == "application/problem+json"


def test_releases_download_from_the_website_with_checksums(client):
    releases = client.get("/site/v1/releases").json()
    assert releases
    for release in releases:
        assert release["download_base"].startswith("/downloads/")
        for filename, info in release["files"].items():
            response = client.get(release["download_base"] + "/" + filename)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert hashlib.sha256(response.content).hexdigest() == info["sha256"]


def test_old_download_links_redirect_permanently(client):
    base = client.get("/api/releases").json()[0]["download_base"]  # the legacy listing: /releases/...
    response = client.get(f"/api{base}/manifest.json", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == base.replace("/releases", "/downloads", 1) + "/manifest.json"
    assert client.get(f"/api{base}/manifest.json").status_code == 200


def test_downloads_recheck_and_refuse_withdrawn_or_unknown_files(cleared, store, tmp_path):
    store.save(cleared)
    write_release(cleared, tmp_path, "2026-10-05", "ng")
    client = TestClient(create_service(store, tmp_path))
    url = "/downloads/2026-10-05/ng/corpus/versions.jsonl"
    assert client.get(url).status_code == 200
    assert client.get("/downloads/2026-10-05/ng/corpus/ng.json").json()["code"] == "unavailable"
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    withdrawn = client.get(url)
    assert withdrawn.status_code == 410 and withdrawn.json()["code"] == "withdrawn"
    assert client.get("/site/v1/releases").json() == []


def test_withdrawal_takes_effect_on_the_next_request(cleared, store, tmp_path):
    store.save(cleared)
    client = TestClient(create_service(store, tmp_path))
    source = cleared.sources[0].id
    assert client.get(f"/site/v1/sources/{source}/versions").json()["items"]
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    assert client.get(f"/site/v1/sources/{source}/versions").json()["items"] == []


def test_legacy_api_unchanged_beside_the_site(client):
    assert client.get("/api/sources").json()["total"] == client.get("/site/v1/sources").json()["total"]
    assert client.get("/api/sources?limit=2&offset=2").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/healthz").json() == {"status": "ok"}


def test_security_txt_follows_rfc_9116_and_is_renewed_before_it_lapses(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DOMAIN", "open.alvary.ai")
    response = client.get("/.well-known/security.txt")
    assert response.status_code == 200 and response.headers["content-type"] == "text/plain; charset=utf-8"
    fields = [line.split(": ", 1) for line in response.text.splitlines()]
    assert fields == [
        ["Contact", "mailto:hello@ruhu.ai"],
        ["Expires", fields[1][1]],
        ["Policy", "https://open.alvary.ai/security#disclosure"],
        ["Canonical", "https://open.alvary.ai/.well-known/security.txt"],  # the API's domain is not live yet
        ["Preferred-Languages", "en"],
    ]
    expires = datetime.strptime(fields[1][1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # RFC 9116 asks for less than a year ahead; renew api/security_txt.EXPIRES before this fails.
    assert timedelta(days=30) < expires - datetime.now(UTC) < timedelta(days=366)
    assert "/security" in client.get("/sitemap.xml").text


def test_service_refuses_to_start_without_the_site_class(monkeypatch, tmp_path, store):
    path = tmp_path / "admission.json"
    values = {
        k: (v.hex() if isinstance(v, bytes) else v) for k, v in config().__dict__.items() if v is not None
    }
    path.write_text(json.dumps(values))
    monkeypatch.setenv("ALVARY_ADMISSION_CONFIG", str(path))
    monkeypatch.delenv("ALVARY_SITE_ADMISSION_CONFIG", raising=False)
    with pytest.raises(ValueError, match="ALVARY_SITE_ADMISSION_CONFIG"):
        create_service(store, tmp_path)


def site_app(store):
    app = FastAPI()
    app.add_middleware(AdmissionMiddleware, config=config(), store=store, route_class="site")

    @app.get("/sources")
    def sources():
        return {"items": []}

    return TestClient(app)


HEADERS = {"X-Forwarded-For": "203.0.113.7"}


def test_site_class_keeps_its_own_buckets_and_ceiling():
    key = "site:" + pseudonym(SECRET, "203.0.113.7").replace(":", "-")
    store = FakeStore([{"admitted": True, "lease_id": "l", "buckets": {key: {"remaining": 9, "reset": 2}}}])
    response = site_app(store).get("/sources", headers=HEADERS)
    assert response.status_code == 200
    request = store.requests[0]
    assert [b["key"] for b in request["buckets"]] == [key]
    assert {c["key"] for c in request["concurrency"]} == {key, "site:global-all"}
    # The database accepts keys of one shape: name, one colon, then letters, digits, - and _.
    shape = re.compile(r"^[a-z]+:[A-Za-z0-9_-]{1,128}$")
    assert all(shape.match(k) for k in [key, "site:global-all", GLOBAL_KEY])
    assert response.headers["open-alvary-quota-tier"] == "site"
    assert response.headers["ratelimit-policy"].startswith('"site"')


def test_site_class_refusals_are_problem_details():
    key = "site:" + pseudonym(SECRET, "203.0.113.7").replace(":", "-")
    store = FakeStore([{"admitted": False, "blocked": [key], "retry_after": 7}])
    response = site_app(store).get("/sources", headers=HEADERS)
    assert response.status_code == 429 and response.headers["retry-after"] == "7"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "rate_limited" and response.json()["retry_after_seconds"] == 7
    down = site_app(FakeStore(["down"])).get("/sources", headers=HEADERS)
    assert down.status_code == 503 and down.json()["code"] == "temporarily_unavailable"


def test_every_response_carries_the_websites_security_headers(client):
    for path in ["/site/v1/sources", "/api/sources", "/healthz", "/site/v1/sources?page=99"]:
        headers = client.get(path).headers
        assert "default-src 'self'" in headers["content-security-policy"], path
        assert headers["strict-transport-security"].startswith("max-age="), path
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin", path
        assert headers["x-content-type-options"] == "nosniff", path


def test_the_built_website_is_served_beside_the_data(catalogue, store, tmp_path):
    store.save(catalogue)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<h1>Open Alvary</h1>")
    client = TestClient(create_service(store, tmp_path, frontend=tmp_path / "web"))
    assert client.get("/").text == "<h1>Open Alvary</h1>"
    assert client.get("/site/v1/sources").status_code == 200


def test_filters_counts_and_sorting(client):
    listing = client.get("/site/v1/sources").json()
    facets = {name: {f["value"]: f for f in values} for name, values in listing["facets"].items()}
    assert facets["jurisdiction"]["ng"]["label"] == "Nigeria" and facets["jurisdiction"]["ng"]["count"] == 4
    assert facets["language"]["en"]["label"] == "English"
    assert facets["type"]["act"]["count"] == 3 and facets["type"]["constitution"]["count"] == 1
    acts = client.get("/site/v1/sources?type=act").json()
    assert acts["total"] == 3 and all(s["document_type"] == "act" for s in acts["items"])
    # A selected filter still shows its alternatives with their counts.
    assert {f["value"] for f in acts["facets"]["type"]} == {"act", "constitution"}
    newest = [s["year"] for s in client.get("/site/v1/sources?sort=newest").json()["items"]]
    assert newest == sorted(newest, reverse=True)
    assert client.get("/site/v1/sources?year=2011").json()["total"] == 1
    assert client.get("/site/v1/search?q=freedom&type=constitution").json()["total"] == 0
    for bad in ["sort=random", "type=a%20b", "year=" + "9" * 70]:
        assert client.get("/site/v1/sources?" + bad).json()["code"] == "invalid_input", bad


def test_search_ranks_titles_first_and_reads_summaries(client):
    found = client.get("/site/v1/search?q=information").json()
    assert found["items"][0]["id"] == SOURCE and found["sort"] == "relevance"
    assert client.get("/site/v1/search?q=fundamental%20rights").json()["total"] == 1  # in a summary


def test_source_record_carries_plain_names(client):
    record = client.get(f"/site/v1/sources/{SOURCE}").json()
    assert record["jurisdiction_name"] == "Nigeria" and record["language_name"] == "English"
    assert record["authority_name"] and record["summary"] and record["year"] == 2011
