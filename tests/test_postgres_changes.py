"""SUB C1: the public change log records what each catalogue write changed, in LIF-02 order, and
serves it as a replayable feed, Atom/RSS windows, coverage and MCP tools (changes-contracts.md)."""

import xml.etree.ElementTree as ET
from datetime import datetime

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
    for unknown in ({"jurisdiction": "ke"}, {"work": "no-such-work"}, {"class": "novel"}, {"language": "xx"}):
        refused = client.get("/changes", params=unknown)
        assert refused.status_code == 400 and refused.json()["detail"] == "The filter is not valid."
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


def test_a_token_keeps_its_filters_after_the_work_they_name_is_withdrawn(feed, cleared):
    store, client = feed
    store.save(cleared)
    work = cleared.sources[0].id
    token = client.get("/changes", params={"work": work}).json()["resume_token"]
    edit(store, lambda c: setattr(next(r for r in c.rights if r.source_id == work), "metadata_public", False))
    # A fresh request may not name a hidden record; the reader's own token still carries on.
    assert client.get("/changes", params={"work": work}).json()["code"] == "invalid_filter"
    later = client.get("/changes", params={"work": work, "resume_token": token}).json()
    assert [e["event_type"] for e in later["events"]] == ["withdrawal"] and later["events"][0]["work"] is None


def test_a_save_still_open_cannot_appear_behind_a_readers_position(feed, cleared):
    from api.store import tables
    from changes.record import publish

    store, client = feed
    store.save(cleared)
    work = cleared.sources[1].id
    sources = tables["sources"]
    with store.engine.connect() as writer:
        transaction = writer.begin()
        payload = writer.scalar(text("SELECT payload FROM sources WHERE id = :i"), {"i": work})
        writer.execute(
            sources.update().where(sources.c.id == work).values(payload=payload | {"summary": "In flight."})
        )
        assert publish(writer, [work]) == 1  # holds the publication revision, not yet committed
        seen = client.get("/changes").json()
        assert all(e["changed_fields"] != ["summary"] for e in seen["events"])
        transaction.commit()
    newer = client.get("/changes", params={"resume_token": seen["resume_token"]}).json()["events"]
    assert [(e["event_type"], e["changed_fields"]) for e in newer] == [("metadata_corrected", ["summary"])]


def test_anonymous_keyed_and_website_routes_answer_the_same_changes(pg_engine, cleared, monkeypatch):
    from test_postgres_api_v1 import PEPPER, TIER, grant, key

    from accounts import policies
    from accounts.store import Accounts
    from api.service import create_service
    from rights.database import PolicyNamespace

    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
    monkeypatch.setenv("ALVARY_KEY_PEPPER", __import__("base64").b64encode(PEPPER).decode())
    for name in (
        "ALVARY_CONSOLE_CLIENT_ID",
        "ALVARY_ADMISSION_CONFIG",
        "ALVARY_SITE_ADMISSION_CONFIG",
        "PUBLIC_DOMAIN",
    ):
        monkeypatch.delenv(name, raising=False)
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    store.save(cleared)
    with pg_engine.connect() as conn:
        accounts = Accounts(
            pg_engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))), key_pepper=PEPPER
        )
    accounts.set_tiers([TIER])
    signed = accounts.sign_in(
        issuer="https://login.example.test/",
        subject="email|a",
        method="email",
        email="a@example.org",
        label="a",
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(signed["id"], "Amina", "Amina")
    grant(accounts, organisation)
    client = TestClient(create_service(store=store))

    def ids(response):
        assert response.status_code == 200, response.text
        return [(e["event_id"], e["event_revision"]) for e in response.json()["events"]]

    anonymous = ids(client.get("/api/changes"))
    assert anonymous and anonymous == ids(client.get("/site/v1/changes"))
    keyed = client.get("/api/v1/changes", headers=key(accounts, signed["id"], organisation))
    assert ids(keyed) == anonymous
    assert client.get("/api/v1/changes").status_code == 401  # the keyed surface still needs a key


def test_atom_and_rss_are_well_formed_and_escape_titles(feed, cleared):
    from email.utils import parsedate_to_datetime

    store, client = feed
    cleared.sources[0].title = 'Rights & "Duties" <Act> 2011'
    store.save(cleared)
    atom = ET.fromstring(client.get("/changes/feed.atom").text)
    a = "{http://www.w3.org/2005/Atom}"
    for name in ("id", "title", "updated", "author"):
        assert atom.find(a + name) is not None, name
    entries = atom.findall(a + "entry")
    assert entries
    for entry in entries:
        assert entry.find(a + "id").text.startswith("urn:uuid:")
        assert entry.find(a + "title").text and entry.find(a + "link").get("href").startswith("http")
        datetime.fromisoformat(entry.find(a + "updated").text.replace("Z", "+00:00"))
    assert any('Rights & "Duties" <Act> 2011' in e.find(a + "title").text for e in entries)

    channel = ET.fromstring(client.get("/changes/feed.rss").text).find("channel")
    for name in ("title", "link", "description"):
        assert channel.find(name).text, name
    for item in channel.findall("item"):
        assert item.find("guid").get("isPermaLink") == "false"
        parsedate_to_datetime(item.find("pubDate").text)


def withdrawals(client):
    return [e for e in client.get("/changes").json()["events"] if e["event_type"] == "withdrawal"]


def test_a_withdrawn_text_version_is_named_with_what_to_do_and_why(feed, cleared):
    store, client = feed
    store.save(cleared)
    work = cleared.sources[0].id
    # The text's approval is withdrawn while the record stays public: a correction, named.
    edit(
        store,
        lambda c: setattr(next(r for r in c.rights if r.source_id == work), "approved_content_hashes", []),
    )
    [withdrawn] = withdrawals(client)
    assert withdrawn["work"]["id"] == work and withdrawn["version"]["id"] == "test-version"
    assert withdrawn["version"]["content_hash"] == cleared.versions[0].content_hash
    assert withdrawn["withdrawal"] == {
        "scope_disclosure": "named",
        "action": "stop_serving",
        "reason_code": "correction",
    }
    assert [e["version_id"] for e in events(store) if e["event_type"] == "withdrawal"] == ["test-version"]


def test_an_expired_rights_review_withdraws_its_text_within_one_worker_run(feed, cleared):
    from sqlalchemy import select, update

    from api.store import tables
    from changes.record import expired

    store, client = feed
    store.save(cleared)
    work = cleared.sources[0].id
    rights = tables["rights"]
    with store.engine.begin() as conn:  # time passes: nothing is written to the catalogue
        payload = conn.scalar(select(rights.c.payload).where(rights.c.source_id == work))
        payload["expires_at"] = "2020-01-01T00:00:00+00:00"
        conn.execute(update(rights).where(rights.c.source_id == work).values(payload=payload))
    assert withdrawals(client) == []  # unseen until something compares the work
    with store.engine.begin() as conn:
        assert expired(conn) == 1
    [withdrawn] = withdrawals(client)
    assert withdrawn["version"]["id"] == "test-version"
    assert withdrawn["withdrawal"]["action"] == "stop_serving"
    assert withdrawn["withdrawal"]["reason_code"] == "rights_changed"
    with store.engine.begin() as conn:
        assert expired(conn) == 0  # already compared: nothing new
