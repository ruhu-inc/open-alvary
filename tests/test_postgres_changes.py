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
from rights.database import PolicyNamespace, provision_roles

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


def test_each_change_to_a_rights_record_is_a_revision_the_text_events_name(feed, cleared, tmp_path):
    from api.v1 import create_v1_app

    store, client = feed
    v1 = TestClient(create_v1_app(store, tmp_path, lambda: []))
    work = cleared.sources[0].id
    baseline = v1.get(f"/rights/{work}/history").json()["revisions"]
    assert [(r["revision"], r["publication_revision"]) for r in baseline] == [(1, 0)]  # the seed's baseline
    store.save(cleared)  # opens the first work's text: its rights record changes
    history = v1.get(f"/rights/{work}/history").json()["revisions"]
    assert [r["revision"] for r in history] == [2, 1] and history[0]["publication_revision"] > 0
    assert history[0]["record"]["approved_content_hashes"] == [cleared.versions[0].content_hash]
    assert history[1]["record"]["approved_content_hashes"] == []
    assert v1.get(f"/rights/{work}").json()["revision"] == 2
    [published] = [
        e for e in client.get("/changes").json()["events"] if e["event_type"] == "text_version_published"
    ]
    assert published["rights_revision"] == 2
    store.save(cleared)  # nothing changed: no new revision
    assert len(v1.get(f"/rights/{work}/history").json()["revisions"]) == 2
    edit(store, lambda c: setattr(next(r for r in c.rights if r.source_id == work), "metadata_public", False))
    assert v1.get(f"/rights/{work}/history").json()["code"] == "unavailable"  # hidden: as if absent


def test_the_rights_history_downgrade_drops_a_baseline_but_never_history(feed, cleared):
    from alembic import command
    from alembic.config import Config

    store, _ = feed  # seeded: each work's rights record is its baseline revision only
    config = Config("alembic.ini")

    def run(step, revision):
        with store.engine.begin() as conn:
            config.attributes["connection"] = conn
            step(config, revision)

    run(command.downgrade, "0028")
    run(command.upgrade, "head")
    store.save(cleared)  # a real revision: now history
    with pytest.raises(RuntimeError, match="public rights history"):
        run(command.downgrade, "0028")


def of_type(client, kind):
    return [e for e in client.get("/changes").json()["events"] if e["event_type"] == kind]


def test_a_checked_repeal_or_amendment_is_a_legal_effect_and_its_correction_keeps_its_id(feed):
    from datetime import date

    from schema.models import Amendment

    store, client = feed
    assert of_type(client, "legal_effect_recorded") == []  # the seeded ISA repeal: a baseline, not news
    link = Amendment(
        id="link-ng-foi-act-2011-amends-ng-constitution-1999",
        source_id="ng-foi-act-2011",
        target_source_id="ng-constitution-1999",
        kind="amends",
        effective_date=date(2011, 5, 28),
        evidence_url="https://example.org/synthetic-amendment",
        verified=True,
    )
    edit(store, lambda c: c.amendments.append(link))
    [effect] = of_type(client, "legal_effect_recorded")
    assert effect["work"]["id"] == "ng-constitution-1999" and effect["schema_version"] == "change-event-1"
    assert (
        effect["legal_effect"]["kind"] == "amendment"
        and effect["legal_effect"]["by"]["id"] == "ng-foi-act-2011"
    )
    assert effect["legal_effect"]["effective_date"] == "2011-05-28"
    assert effect["legal_effective"] == {"from": "2011-05-28", "precision": "day"}
    assert effect["event_revision"] == 1 and effect["supersedes"] is None
    # A correction of the link is the same event's next revision, superseding the first.
    edit(
        store,
        lambda c: setattr(
            next(a for a in c.amendments if a.id == link.id), "effective_date", date(2011, 6, 1)
        ),
    )
    [first, corrected] = of_type(client, "legal_effect_recorded")
    assert corrected["event_id"] == first["event_id"] and corrected["event_revision"] == 2
    assert corrected["change_reason"] == "corrected"
    assert corrected["supersedes"] == {"event_id": first["event_id"], "event_revision": 1}
    assert corrected["legal_effect"]["effective_date"] == "2011-06-01"
    # An unchecked link is not news; nothing is said of a record that is no longer public.
    unchecked = link.model_copy(
        update={"id": "link-unchecked", "source_id": "ng-cbn-act-2007", "verified": False}
    )
    edit(store, lambda c: c.amendments.append(unchecked))
    assert len(of_type(client, "legal_effect_recorded")) == 2
    edit(
        store,
        lambda c: setattr(
            next(r for r in c.rights if r.source_id == "ng-foi-act-2011"), "metadata_public", False
        ),
    )
    assert of_type(client, "legal_effect_recorded") == []
    # The link left public view with its law: recorded as the event's withdrawal, shown only while public.
    [*_, gone] = [e for e in events(store) if e["event_type"] == "legal_effect_recorded"]
    assert gone["event_revision"] == 3 and gone["payload"]["legal_effect"]["review_state"] == "withdrawn"


DIRECTIVE = "aa-ecowas-synthetic-directive"
TRANSPOSITION = f"link-ng-foi-act-2011-transposes-{DIRECTIVE}"


def with_directive(corpus):
    """A synthetic ECOWAS directive (test only, not a real instrument), public like the Nigerian records,
    and a checked link saying the Freedom of Information Act transposes it."""
    from datetime import date

    from schema.models import Amendment, Authority, Jurisdiction, Source

    corpus.jurisdictions.append(
        Jurisdiction(id="aa-ecowas", name="Economic Community of West African States", country_code="AA")
    )
    corpus.authorities.append(
        Authority(
            id="aa-ecowas-synthetic",
            jurisdiction_id="aa-ecowas",
            name="Synthetic regional authority",
            kind="other",
            official_url="https://example.org/synthetic-authority",
        )
    )
    corpus.sources.append(
        Source(
            id=DIRECTIVE,
            jurisdiction_id="aa-ecowas",
            authority_id="aa-ecowas-synthetic",
            title="Synthetic Directive on Access to Information",
            document_type="other",
            canonical_url="https://example.org/synthetic-directive",
            citation="Synthetic Directive 1/2010",
        )
    )
    rights = next(r for r in corpus.rights if r.source_id == "ng-foi-act-2011")
    corpus.rights.append(rights.model_copy(update={"id": f"rights-{DIRECTIVE}", "source_id": DIRECTIVE}))
    corpus.amendments.append(
        Amendment(
            id=TRANSPOSITION,
            source_id="ng-foi-act-2011",
            target_source_id=DIRECTIVE,
            kind="transposes",
            effective_date=date(2011, 5, 28),
            evidence_url="https://example.org/synthetic-transposition",
            verified=True,
        )
    )


def test_a_transposition_is_an_effect_on_each_work_and_its_withdrawal_corrects_both(feed, tmp_path):
    from api.v1 import create_v1_app

    store, client = feed
    edit(store, with_directive)
    effects = of_type(client, "legal_effect_recorded")
    assert sorted(e["work"]["id"] for e in effects) == [DIRECTIVE, "ng-foi-act-2011"]  # one each
    on = {e["work"]["id"]: e for e in effects}
    assert on[DIRECTIVE]["legal_effect"]["kind"] == "transposition"
    assert on[DIRECTIVE]["legal_effect"]["by"]["id"] == "ng-foi-act-2011"
    assert on[DIRECTIVE]["summary"].endswith("was transposed by Freedom of Information Act 2011.")
    assert on["ng-foi-act-2011"]["legal_effect"]["of"] == {
        "id": DIRECTIVE,
        "title": "Synthetic Directive on Access to Information",
        "citation": "Synthetic Directive 1/2010",
    }
    assert "by" not in on["ng-foi-act-2011"]["legal_effect"]
    assert all(e["legal_effective"] == {"from": "2011-05-28", "precision": "day"} for e in effects)
    # Each side lists the other.
    api = TestClient(create_v1_app(store, tmp_path, lambda: []))

    def related(work):
        page = api.get(f"/sources/{work}/relationships").json()
        return [(i["kind"], i["source_id"]) for i in page["items"]]

    assert related("ng-foi-act-2011") == [("transposes", DIRECTIVE)]
    assert related(DIRECTIVE) == [("transposed_by", "ng-foi-act-2011")]
    assert api.get(f"/sources/{DIRECTIVE}/relationships", params={"kind": "transposes"}).json()["items"] == []

    # Withdrawn (no longer checked), each work's event is corrected once, naming the link.
    edit(store, lambda c: setattr(next(a for a in c.amendments if a.id == TRANSPOSITION), "verified", False))
    corrections = of_type(client, "legal_effect_recorded")[2:]
    assert sorted(e["work"]["id"] for e in corrections) == [DIRECTIVE, "ng-foi-act-2011"]
    for correction in corrections:
        first = on[correction["work"]["id"]]
        assert correction["event_id"] == first["event_id"] and correction["event_revision"] == 2
        assert correction["change_reason"] == "corrected"
        assert correction["supersedes"] == {"event_id": first["event_id"], "event_revision": 1}
        assert correction["legal_effect"]["review_state"] == "withdrawn"
        assert correction["legal_effect"]["kind"] == "transposition"
        assert correction["summary"] == (
            "The recorded transposition of Synthetic Directive on Access to Information by Freedom of"
            " Information Act 2011 was withdrawn."
        )
    assert {
        e["work"]["id"]: e["legal_effect"].get("by", e["legal_effect"].get("of"))["id"] for e in corrections
    } == {
        DIRECTIVE: "ng-foi-act-2011",
        "ng-foi-act-2011": DIRECTIVE,
    }
    assert related("ng-foi-act-2011") == [] and related(DIRECTIVE) == []
    assert sweep_count(store) == 0  # announced once
    # Checked again, the same events are corrected again.
    edit(store, lambda c: setattr(next(a for a in c.amendments if a.id == TRANSPOSITION), "verified", True))
    restored = of_type(client, "legal_effect_recorded")[4:]
    assert sorted((e["work"]["id"], e["event_revision"]) for e in restored) == [
        (DIRECTIVE, 3),
        ("ng-foi-act-2011", 3),
    ]
    assert all("review_state" not in e["legal_effect"] for e in restored)


def test_a_withdrawn_repeal_corrects_its_one_event_whatever_withdrew_it(feed):
    from datetime import date

    from schema.models import Amendment

    store, client = feed
    link = Amendment(
        id="link-ng-foi-act-2011-repeals-ng-cbn-act-2007",
        source_id="ng-foi-act-2011",
        target_source_id="ng-cbn-act-2007",
        kind="repeals",
        effective_date=date(2011, 5, 28),
        evidence_url="https://example.org/synthetic-repeal",
        verified=True,
    )
    edit(store, lambda c: c.amendments.append(link))
    [first] = of_type(client, "legal_effect_recorded")
    assert first["work"]["id"] == "ng-cbn-act-2007" and first["legal_effect"]["kind"] == "repeal"
    assert (
        first["summary"]
        == "Central Bank of Nigeria Act 2007 was repealed by Freedom of Information Act 2011."
    )
    assert "review_state" not in first["legal_effect"]
    # Deleted outright, the link's event is corrected on the repealed law only.
    with store.engine.begin() as conn:
        conn.execute(text("DELETE FROM amendments WHERE id = :id"), {"id": link.id})
        sweep(conn)
    [_, withdrawn] = of_type(client, "legal_effect_recorded")
    assert withdrawn["event_id"] == first["event_id"] and withdrawn["event_revision"] == 2
    assert (
        withdrawn["work"]["id"] == "ng-cbn-act-2007"
        and withdrawn["legal_effect"]["review_state"] == "withdrawn"
    )
    assert withdrawn["legal_effect"]["by"]["id"] == "ng-foi-act-2011"
    assert withdrawn["summary"] == (
        "The recorded repeal of Central Bank of Nigeria Act 2007 by Freedom of Information Act 2011 was withdrawn."
    )
    # Recorded again as an amendment instead: a new link, with its own event.
    edit(
        store, lambda c: c.amendments.append(link.model_copy(update={"id": "link-amends", "kind": "amends"}))
    )
    *_, amended = of_type(client, "legal_effect_recorded")
    assert amended["event_id"] != first["event_id"] and amended["legal_effect"]["kind"] == "amendment"
    assert len(of_type(client, "legal_effect_recorded")) == 3


def test_a_status_correction_says_what_it_was_and_only_for_fields_never_withheld(feed):
    store, client = feed
    work = "ng-foi-act-2011"
    edit(store, lambda c: setattr(next(s for s in c.sources if s.id == work), "legal_status", "current"))
    [status] = of_type(client, "metadata_corrected")
    assert status["changed_fields"] == ["legal_status"] and status["previous"] == {"legal_status": "unknown"}
    edit(store, lambda c: setattr(next(s for s in c.sources if s.id == work), "summary", "A new summary."))
    summary = of_type(client, "metadata_corrected")[-1]
    assert summary["changed_fields"] == ["summary"] and summary["previous"] == {}


def test_a_text_that_replaces_another_names_the_provisions_it_adds_removes_and_changes(feed, cleared):
    from hashlib import sha256

    from schema.models import StructureNode

    store, client = feed
    store.save(cleared)  # the first text: one provision, "section/1"
    work = cleared.sources[0].id
    head = "Synthetic fixture — not legislation.\nSection 1. Test changed."
    text_ = head + "\nSection 2. Added.\n"
    digest = sha256(text_.encode()).hexdigest()
    replacement = cleared.versions[0].model_copy(
        update={
            "id": "test-version-2",
            "canonical_text": text_,
            "content_hash": digest,
            "prior_version_id": "test-version",
        }
    )

    def node(id_, locator, start, end):
        return StructureNode(
            id=id_, source_id=work, version_id="test-version-2", kind="section", locator=locator,
            order=0, start_byte=start, end_byte=end, source_hash=digest,
        )  # fmt: skip

    nodes = [
        node("test-node-2-1", "section/1", 0, len(head.encode())),
        node("test-node-2-2", "section/2", len(head.encode()) + 1, len(text_.encode()) - 1),
    ]

    def publish_replacement(corpus):
        corpus.versions.append(replacement)
        corpus.structure.extend(nodes)
        rights = next(r for r in corpus.rights if r.source_id == work)
        rights.approved_content_hashes = [*rights.approved_content_hashes, digest]

    edit(store, publish_replacement)
    [event] = [e for e in of_type(client, "text_version_published") if e["version"]["id"] == "test-version-2"]
    assert event["change_reason"] == "extraction_correction"  # read again from the same original file
    assert event["work"]["legal_status"] == "unknown"  # as the catalogue has it now
    assert event["version_diff"] == {
        "prior_version_id": "test-version",
        "added": ["section/2"],
        "removed": [],
        "changed": ["section/1"],
        "truncated": False,
    }


def test_a_country_losing_its_last_record_or_gaining_its_first_is_a_coverage_change(feed):
    store, client = feed

    def every_record(public):
        def change(corpus):
            for rights in corpus.rights:
                rights.metadata_public = public

        return change

    edit(store, every_record(False))
    [lost] = [e for e in events(store) if e["event_type"] == "coverage_changed"]
    assert lost["event_revision"] == 1 and lost["supersedes_event_id"] is None
    assert lost["payload"]["schema_version"] == "change-event-1"
    assert lost["payload"]["coverage"] == {"has_public_records": False}

    edit(store, every_record(True))
    gained = [e for e in events(store) if e["event_type"] == "coverage_changed"][-1]
    assert gained["event_id"] != lost["event_id"]
    assert gained["payload"]["coverage"] == {"has_public_records": True}
    served = of_type(client, "coverage_changed")
    assert served and all(e["schema_version"] == "change-event-1" for e in served)


# Migrations 0029-0031, 0034 and 0035 grant their tables only to capability roles that already exist;
# the provision bundles v0029-v0031, v0034 and v0035 grant the same to roles created afterwards. A key
# without a schema is in the changes schema.
LATER_TABLES = {
    "rights_revision": {"release": {"SELECT", "INSERT"}, "serving": {"SELECT"}},
    "link_state": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "text_unit": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "text_index_state": {"release": {"SELECT", "UPDATE"}, "serving": {"SELECT"}},
    "document_unit": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "event_state": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "index_generation": {"release": {"SELECT", "INSERT", "UPDATE"}, "serving": {"SELECT"}},
    "index_job": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "engine_state": {"release": {"SELECT", "INSERT", "UPDATE"}, "serving": {"SELECT"}},
    "membership_state": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "suspension_state": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    "treatment_state": {"release": {"SELECT", "INSERT", "UPDATE", "DELETE"}, "serving": {"SELECT"}},
    # The guard reads every table of the corpus schema (bundle v0003).
    "corpus.citation": {
        "release": {"SELECT", "INSERT", "UPDATE"},
        "serving": {"SELECT"},
        "guard": {"SELECT"},
    },
    "corpus.judicial_treatment": {
        "release": {"SELECT", "INSERT", "UPDATE"},
        "serving": {"SELECT"},
        "guard": {"SELECT"},
    },
    "corpus.membership": {
        "release": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "serving": {"SELECT"},
        "guard": {"SELECT"},
    },
    "corpus.membership_suspension": {
        "release": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "serving": {"SELECT"},
        "guard": {"SELECT"},
    },
    "corpus.legal_event": {
        "release": {"SELECT", "INSERT", "UPDATE"},
        "serving": {"SELECT"},
        "guard": {"SELECT"},
    },
    "corpus.currentness_profile": {
        "release": {"SELECT", "INSERT", "UPDATE"},
        "serving": {"SELECT"},
        "guard": {"SELECT"},
    },
}


@pytest.mark.fresh_migrations
def test_roles_provisioned_after_migrating_get_exactly_the_later_change_log_grants(pg_engine):
    migrate(pg_engine)
    with pg_engine.begin() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        roles = provision_roles(conn, ns)
    try:
        with pg_engine.connect() as conn:
            privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
            if conn.scalar(text("SELECT current_setting('server_version_num')::int")) >= 170000:
                privileges.append("MAINTAIN")
            held = {
                table: {
                    kind: {
                        privilege
                        for privilege in privileges
                        if conn.scalar(
                            text("SELECT has_table_privilege(:role, :table, :privilege)"),
                            {
                                "role": role,
                                "table": ns.qualified(
                                    conn, *(table.split(".") if "." in table else ("changes", table))
                                ),
                                "privilege": privilege,
                            },
                        )
                    }
                    for kind, role in roles.items()
                }
                for table in LATER_TABLES
            }
            for kind in ("release", "serving"):
                assert conn.scalar(
                    text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"),
                    {"role": roles[kind], "schema": ns.schema("changes")},
                )
        assert held == {
            table: {kind: expected.get(kind, set()) for kind in roles}
            for table, expected in LATER_TABLES.items()
        }
    finally:
        with pg_engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote_identifier
            for kind in ("policy", "staging", "corpus"):
                conn.execute(text(f"DROP SCHEMA {quote(ns.schema(kind))} CASCADE"))
            for role in roles.values():
                conn.execute(text(f"DROP OWNED BY {quote(role)}"))
                conn.execute(text(f"DROP ROLE {quote(role)}"))
