"""SUB C1: the public change log records what each catalogue write changed, in LIF-02 order, and
serves it as a replayable feed, Atom/RSS windows, coverage and MCP tools (changes-contracts.md)."""

import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.main import create_app
from api.store import Store, seed
from changes import admin
from changes.record import namespace, sweep

pytestmark = pytest.mark.postgres


@pytest.fixture
def feed(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    return store, TestClient(create_app(store=store))


def events(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT * FROM {table} ORDER BY publication_revision, event_id, event_revision")
            ).mappings()
        ]


def edit(store, change):
    corpus = store.load()
    change(corpus)
    store.save(corpus)


def test_the_first_publication_is_a_baseline_and_reseeding_changes_nothing(feed):
    store, client = feed
    assert events(store) == []
    seed(store)
    assert events(store) == []
    coverage = client.get("/change-coverage").json()
    assert coverage["feed_started_at"] and [j["id"] for j in coverage["jurisdictions"]] == ["ng"]
    assert client.get("/changes").json()["events"] == []


def test_each_kind_of_change_is_one_event_and_one_save_shares_a_revision(feed, cleared):
    store, client = feed
    store.save(cleared)  # opens text and changes the rights status of the first source
    first = events(store)
    assert {e["event_type"] for e in first} == {"text_version_published", "metadata_corrected"}
    assert len({e["publication_revision"] for e in first}) == 1
    text_event = next(e for e in first if e["event_type"] == "text_version_published")
    assert text_event["change_reason"] == "first_text" and text_event["version_id"] == "test-version"
    corrected = next(e for e in first if e["event_type"] == "metadata_corrected")
    assert corrected["payload"]["changed_fields"] == ["rights_status"]

    work = cleared.sources[1].id
    edit(store, lambda c: setattr(next(s for s in c.sources if s.id == work), "summary", "A new summary."))
    [summary] = events(store)[2:]
    assert summary["event_type"] == "metadata_corrected" and summary["payload"]["changed_fields"] == [
        "summary"
    ]
    assert summary["publication_revision"] > first[0]["publication_revision"]

    # Withdrawn, the record is not named; its earlier events disappear from the feed.
    edit(store, lambda c: setattr(next(r for r in c.rights if r.source_id == work), "metadata_public", False))
    page = client.get("/changes").json()
    withdrawn = [e for e in page["events"] if e["event_type"] == "withdrawal"]
    assert len(withdrawn) == 1 and withdrawn[0]["work"] is None
    assert withdrawn[0]["withdrawal"]["scope_disclosure"] == "revalidate_releases"
    assert all((e["work"] or {}).get("id") != work for e in page["events"])
    # Restored, it is added again and named again.
    edit(store, lambda c: setattr(next(r for r in c.rights if r.source_id == work), "metadata_public", True))
    restored = events(store)[-1]
    assert restored["event_type"] == "catalogue_added" and restored["change_reason"] == "restored"
    assert sweep_count(store) == 0


def sweep_count(store):
    with store.engine.begin() as conn:
        return sweep(conn)


def test_pages_cursors_resume_tokens_and_their_refusals(feed, cleared):
    store, client = feed
    store.save(cleared)
    first = client.get("/changes", params={"limit": 1}).json()
    assert len(first["events"]) == 1 and first["has_more"] and first["next_cursor"]
    second = client.get("/changes", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["events"][0]["event_id"] != first["events"][0]["event_id"]
    rest = client.get("/changes", params={"limit": 1, "cursor": second["next_cursor"]}).json()
    assert rest["events"] == [] and not rest["has_more"]
    # From the window's resume token, only newer changes arrive.
    assert client.get("/changes", params={"resume_token": rest["resume_token"]}).json()["events"] == []
    edit(store, lambda c: setattr(c.sources[1], "summary", "Changed again."))
    newer = client.get("/changes", params={"resume_token": rest["resume_token"]}).json()["events"]
    assert [e["event_type"] for e in newer] == ["metadata_corrected"]
    now = client.get("/changes", params={"start": "now"}).json()
    assert now["events"] == [] and now["resume_token"]

    tampered = first["next_cursor"][:-4] + "AAAA"
    assert client.get("/changes", params={"limit": 1, "cursor": tampered}).json()["code"] == "invalid_cursor"
    other = client.get("/changes", params={"limit": 1, "cursor": first["next_cursor"], "type": "withdrawal"})
    assert other.status_code == 400 and other.json()["code"] == "invalid_cursor"
    assert client.get("/changes", params={"colour": "red"}).json()["code"] == "invalid_input"
    assert client.get("/changes", params={"issuer": "x"}).json()["code"] == "unsupported_filter"


def test_a_new_epoch_tells_clients_to_reconcile(feed, cleared, monkeypatch):
    store, client = feed
    store.save(cleared)
    token = client.get("/changes").json()["resume_token"]
    monkeypatch.setattr(admin, "engine_for", lambda: store.engine)
    assert admin.main(["new-epoch", "--reason", "Restore drill"]) == 0
    reset = client.get("/changes", params={"resume_token": token})
    assert reset.status_code == 409 and reset.json()["code"] == "feed_reset"


def test_filters_single_events_feeds_and_mcp(feed, cleared):
    store, client = feed
    store.save(cleared)
    texts = client.get("/changes", params={"type": "text_version_published"}).json()["events"]
    assert len(texts) == 1 and texts[0]["version"]["id"] == "test-version"
    assert texts[0]["work"]["url"].endswith(f"/sources/{cleared.sources[0].id}")
    assert client.get("/changes", params={"jurisdiction": "ke"}).json()["events"] == []
    assert len(client.get("/changes", params={"jurisdiction": "ng"}).json()["events"]) == 2
    one = client.get(f"/changes/{texts[0]['event_id']}")
    assert one.json()["event_type"] == "text_version_published"
    assert client.get("/changes/00000000-0000-4000-8000-000000000000").status_code == 404

    atom = client.get("/changes/feed.atom")
    assert atom.headers["content-type"].startswith("application/atom+xml")
    assert len(ET.fromstring(atom.text).findall("{http://www.w3.org/2005/Atom}entry")) == 2
    rss = ET.fromstring(client.get("/changes/feed.rss", params={"type": "metadata_corrected"}).text)
    assert len(rss.findall("./channel/item")) == 1

    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert {"list_changes", "get_change"} <= {t["name"] for t in tools["result"]["tools"]}
    listed = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_changes", "arguments": {"type": ["text_version_published"]}},
        },
    ).json()["result"]
    assert not listed["isError"] and "test-version" in listed["content"][0]["text"]


def test_the_change_log_is_append_only(feed, cleared):
    store, _ = feed
    store.save(cleared)
    with pytest.raises(Exception, match="append_only"), store.engine.begin() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        conn.execute(text(f"UPDATE {table} SET work_id = 'x'"))


def test_without_shared_keys_a_deployment_turns_replay_off_but_keeps_its_pages(feed, monkeypatch):
    from changes import routes

    store, client = feed
    monkeypatch.setenv("ALVARY_RUNTIME_MODE", "persistent")
    monkeypatch.delenv("ALVARY_FEED_KEYS", raising=False)
    routes.tokens.cache_clear()
    try:
        assert client.get("/changes").json()["code"] == "temporarily_unavailable"
        assert client.get("/changes/recent").status_code == 200
        assert client.get("/changes/feed.atom").status_code == 200
        assert client.get("/change-coverage").status_code == 200
    finally:
        routes.tokens.cache_clear()
