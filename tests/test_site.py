"""MAC-01/02 activation A: the website's own data path, public downloads and the legacy API beside them."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.admission import GLOBAL_KEY, AdmissionMiddleware, pseudonym
from api.service import SECURITY_HEADERS, create_service
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
    write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    client = TestClient(create_service(store, tmp_path))
    url = "/downloads/2026-10-05/ng/metadata/sources.jsonl"
    assert client.get(url).status_code == 200
    assert client.get("/downloads/2026-10-05/ng/metadata/ng.json").json()["code"] == "unavailable"
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    withdrawn = client.get(url)
    assert withdrawn.status_code == 410 and withdrawn.json()["code"] == "withdrawn"
    assert client.get("/site/v1/releases").json() == []


def test_a_release_holding_full_text_is_never_listed_or_served(cleared, store, tmp_path):
    store.save(cleared)
    write_release(cleared, tmp_path, "2026-10-05", "ng")  # corpus mode: holds the cleared text
    write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    client = TestClient(create_service(store, tmp_path))
    assert [manifest["mode"] for manifest in client.get("/site/v1/releases").json()] == ["metadata"]
    for name in ("versions.jsonl", "manifest.json"):
        refused = client.get(f"/downloads/2026-10-05/ng/corpus/{name}")
        assert refused.status_code == 404 and refused.json()["code"] == "unavailable"
    assert client.get("/downloads/2026-10-05/ng/metadata/manifest.json").status_code == 200


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


def test_no_answer_lets_a_page_address_such_as_a_search_reach_another_site(catalogue, store, tmp_path):
    """Referrer-Policy on every answer the public service sends: the website's pages (a search page's
    address holds its ?q=), refusals and failures included (owner, 2026-10-21)."""
    store.save(catalogue)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<h1>Open Alvary</h1>")
    app = create_service(store, tmp_path, frontend=tmp_path / "web")

    @app.get("/failing")
    def failing():
        raise RuntimeError("private-error-content")

    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the website's catch-all
    client = TestClient(app, raise_server_exceptions=False)
    paths = {
        "/": 200,
        "/search?q=data+protection": 200,
        f"/sources/{SOURCE}": 200,
        "/missing.js": 404,
        "/site/v1/search?q=data+protection": 200,
        "/site/v1/missing": 404,
        "/robots.txt": 200,
        "/failing": 500,
    }
    answers = {path: client.get(path) for path in paths} | {"POST /": client.post("/")}
    assert {path: answer.status_code for path, answer in answers.items()} == paths | {"POST /": 405}
    policies = {path: answer.headers.get("referrer-policy") for path, answer in answers.items()}
    assert policies == {path: "strict-origin-when-cross-origin" for path in answers}
    failed = answers["/failing"]
    assert {name: failed.headers.get(name) for name in SECURITY_HEADERS} == SECURITY_HEADERS
    assert "private-error-content" not in failed.text


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
    assert facets["jurisdiction"]["ng"]["label"] == "Nigeria" and facets["jurisdiction"]["ng"]["count"] == 5
    assert facets["language"]["en"]["label"] == "English"
    assert facets["type"]["act"]["count"] == 4 and facets["type"]["constitution"]["count"] == 1
    acts = client.get("/site/v1/sources?type=act").json()
    assert acts["total"] == 4 and all(s["document_type"] == "act" for s in acts["items"])
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


def test_a_records_history_lists_checked_links_both_ways_and_the_law_in_force_now(catalogue, store, tmp_path):
    from schema.models import Amendment

    later = next(s for s in catalogue.sources if s.id == "ng-isa-2025")
    newest = later.model_copy(
        update={"id": "ng-isa-2030", "title": "Investments and Securities Act 2030", "year": 2030}
    )
    rights = next(r for r in catalogue.rights if r.source_id == later.id)
    catalogue.sources.append(newest)
    catalogue.rights.append(rights.model_copy(update={"id": "rights-ng-isa-2030", "source_id": newest.id}))
    evidence = "https://www.sec.gov.ng/documents/1319/Investments_and_Securities_Act_2025_x9rSXtI.pdf"

    def link(source, kind, target, verified=True):
        return Amendment(id=f"{source}-{kind}-{target}", source_id=source, target_source_id=target, kind=kind,
                         evidence_url=evidence, verified=verified)  # fmt: skip

    catalogue.amendments = [
        link("ng-isa-2025", "repeals", "ng-isa-2007"),
        link("ng-isa-2030", "repeals", "ng-isa-2025"),
        link("ng-cbn-act-2007", "made_under", "ng-constitution-1999"),
        link("ng-foi-act-2011", "amends", "ng-isa-2007", verified=False),  # not checked: not shown
    ]
    store.save(catalogue)
    client = TestClient(create_service(store, tmp_path))
    first = client.get("/site/v1/sources/ng-isa-2007/relationships").json()
    assert first["legal_status"] == "repealed"
    assert first["current"] == {
        "source_id": "ng-isa-2030",
        "title": newest.title,
        "citation": newest.citation,
    }
    assert [(r["kind"], r["source_id"], r["verified"]) for r in first["items"]] == [
        ("repealed_by", "ng-isa-2025", True)
    ]
    middle = client.get("/site/v1/sources/ng-isa-2025/relationships").json()
    assert {(r["kind"], r["source_id"]) for r in middle["items"]} == {
        ("repeals", "ng-isa-2007"),
        ("repealed_by", "ng-isa-2030"),
    }
    assert middle["current"]["source_id"] == "ng-isa-2030"
    constitution = client.get("/site/v1/sources/ng-constitution-1999/relationships").json()
    assert (
        constitution["items"] == []
        and constitution["enables_total"] == 1
        and "next_cursor" not in constitution
    )
    assert constitution["enables"][0]["kind"] == "enables" and constitution["current"] is None
    # A record withdrawn from public view drops out of the others' history.
    store.save(catalogue.model_copy(update={"rights": [
        r.model_copy(update={"metadata_public": False}) if r.source_id == "ng-isa-2030" else r for r in catalogue.rights
    ]}))  # fmt: skip
    assert (
        client.get("/site/v1/sources/ng-isa-2007/relationships").json()["current"]["source_id"]
        == "ng-isa-2025"
    )
    assert client.get("/site/v1/sources/ng-isa-2030/relationships").json()["code"] == "unavailable"


def test_a_laws_many_instruments_come_a_page_at_a_time_and_agents_get_the_same(catalogue, store, tmp_path):
    from api.catalogue import InvalidSearch, LegacyCatalogue
    from api.main import create_app
    from api.repositories import LegacyRepository
    from schema.models import Amendment

    law = next(s for s in catalogue.sources if s.id == "ng-isa-2025")
    rights = next(r for r in catalogue.rights if r.source_id == law.id)
    for n in range(30):
        order = law.model_copy(update={"id": f"ng-order-{n:02d}", "title": f"Order {n}", "year": 2026})
        catalogue.sources.append(order)
        catalogue.rights.append(rights.model_copy(update={"id": f"rights-{order.id}", "source_id": order.id}))
        catalogue.amendments.append(
            Amendment(
                id=f"made-{n:02d}",
                source_id=order.id,
                target_source_id=law.id,
                kind="made_under",
                evidence_url="https://example.org/order",
                verified=True,
            )  # fmt: skip
        )
    catalogue.amendments = [a.model_copy(update={"verified": True}) for a in catalogue.amendments]
    store.save(catalogue)
    with store.engine.connect() as connection:
        service = LegacyCatalogue(LegacyRepository(connection))
        first = service.relationships(law.id)
        assert first.total == 31 and len(first.items) == 25  # its repeal first, then the orders
        assert [i.kind for i in first.items[:2]] == ["repeals", "enables"] and first.next_cursor
        second = service.relationships(law.id, cursor=first.next_cursor)
        assert [i.source_id for i in second.items] == [f"ng-order-{n:02d}" for n in range(24, 30)]
        assert second.next_cursor is None
        orders = service.relationships(law.id, kinds=["enables"], limit=100)
        assert orders.total == 30 and {i.kind for i in orders.items} == {"enables"}
        for bad in ({"cursor": "not-a-cursor"}, {"limit": 101}, {"kinds": ["parent_of"]}):
            with pytest.raises(InvalidSearch):
                service.relationships(law.id, **bad)
    agent = TestClient(create_app(store, tmp_path))
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    listed = agent.post("/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "tools/list"}).json()
    assert "get_relationships" in {t["name"] for t in listed["result"]["tools"]}
    answer = agent.post(
        "/mcp",
        json=call | {"params": {"name": "get_relationships", "arguments": {"source_id": "ng-isa-2007"}}},
    ).json()["result"]
    history = json.loads(answer["content"][0]["text"])
    assert history["current"]["source_id"] == "ng-isa-2025" and history["items"][0]["kind"] == "repealed_by"
    refused = agent.post(
        "/mcp",
        json=call | {"params": {"name": "get_relationships", "arguments": {"source_id": law.id, "limit": 0}}},
    ).json()
    assert refused["error"]["code"] == -32602


def test_the_reviewed_vocabulary_is_served_from_both_ends_in_the_readers_order(catalogue, store, tmp_path):
    from api.catalogue import LegacyCatalogue
    from api.repositories import LegacyRepository
    from schema.models import Amendment

    kinds = ("commences", "transposes", "domesticates", "implements", "ratifies", "cites")
    catalogue.amendments += [
        Amendment(id=f"link-{kind}", source_id="ng-foi-act-2011", target_source_id="ng-cbn-act-2007", kind=kind,
                  evidence_url="https://example.org/synthetic-link", verified=True)
        for kind in kinds
    ]  # fmt: skip
    store.save(catalogue)
    with store.engine.connect() as connection:
        service = LegacyCatalogue(LegacyRepository(connection))
        forward = [i.kind for i in service.relationships("ng-foi-act-2011").items]
        backward = [i.kind for i in service.relationships("ng-cbn-act-2007").items]
        assert forward == ["transposes", "domesticates", "implements", "ratifies", "commences", "cites"]
        assert backward == ["commenced_by", "transposed_by", "domesticated_by", "implemented_by",
                            "ratified_by", "cited_by"]  # fmt: skip
        assert [i.source_id for i in service.relationships("ng-cbn-act-2007", kinds=["cited_by"]).items] == [
            "ng-foi-act-2011"
        ]
    site = TestClient(create_service(store, tmp_path)).get("/site/v1/sources/ng-cbn-act-2007/relationships")
    assert [i["kind"] for i in site.json()["items"]] == backward


def test_behind_front_door_only_its_requests_are_served(catalogue, store, tmp_path, monkeypatch):
    store.save(catalogue)
    monkeypatch.setenv("ALVARY_FRONT_DOOR_ID", "fd-1")
    client = TestClient(create_service(store, tmp_path))
    refused = client.get("/site/v1/project")
    assert refused.status_code == 403 and refused.json()["code"] == "wrong_address"
    assert refused.headers["x-content-type-options"] == "nosniff"  # still with the security headers
    assert client.get("/site/v1/project", headers={"X-Azure-FDID": "fd-2"}).status_code == 403
    assert client.get("/site/v1/project", headers={"X-Azure-FDID": "fd-1"}).status_code == 200
    assert client.get("/healthz").status_code == 200  # the platform's probes come straight in
