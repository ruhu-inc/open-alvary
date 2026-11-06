"""Approved works reach the public catalogue as records, with their changes in the change log; a work
whose approval lapses is taken down (operations.publish)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_publish import both_decisions as both_decisions  # noqa: F401  (fixture)
from test_publish import decisions as decisions  # noqa: F401  (fixture)
from test_publish import rwanda_law

from api.main import create_app
from api.store import Store, seed
from api.v1 import create_v1_app
from changes import admin
from changes.record import namespace
from operations import publish
from rights.policy import public_corpus

pytestmark = pytest.mark.postgres
WORK = "rw-law-055-2024"


@pytest.fixture
def store(pg_engine):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)  # Nigeria, and the change log's baseline
    return store


def events(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            (r.event_type, r.work_id)
            for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY publication_revision, event_id"))
        ]


def test_an_approved_work_is_published_as_a_record_and_taken_down_when_approval_lapses(store, decisions):
    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")
    approved = [publish.Approved(WORK, rwanda_law(), ["en", "fr", "rw"], "Ada Reviewer", datetime.now(UTC))]
    dry = publish.publish_records(store, "rw", approved, set(), decision, dry_run=True)
    assert dry["published"] == [WORK] and events(store) == []
    report = publish.publish_records(store, "rw", approved, set(), decision)
    assert report["published"] == [WORK]
    # Its rights record's first revision is announced with it.
    assert sorted(events(store)) == [
        ("catalogue_added", WORK),
        ("coverage_changed", "rw"),
        ("rights_changed", WORK),
    ]
    client = TestClient(create_app(store=store))
    record = client.get(f"/sources/{WORK}").json()
    assert record["title"].startswith("Law n° 055/2024") and record["has_full_text"] is False
    assert client.get(f"/sources/{WORK}/versions").json()["items"] == []  # never any text
    assert {"id": "rw", "name": "Rwanda"}.items() <= next(
        j for j in client.get("/jurisdictions").json() if j["id"] == "rw"
    ).items()
    # Publishing again changes nothing; a lapsed approval takes the record down.
    assert publish.publish_records(store, "rw", approved, set(), decision)["updated"] == [WORK]
    assert len(events(store)) == 3
    assert publish.publish_records(store, "rw", [], {WORK}, decision)["taken_down"] == [WORK]
    # One save, one revision: the withdrawal and Rwanda losing its last public record (any order).
    assert set(events(store)[3:]) == {("withdrawal", WORK), ("coverage_changed", "rw")}
    assert client.get(f"/sources/{WORK}").status_code == 404


def test_the_workbench_approval_is_what_publishes(store, decisions, tmp_path, monkeypatch):
    from test_workbench import ACT, approve, confirm_details, flag_id, save, signed_in
    from test_workbench import world as workbench_world  # noqa: F401

    from schema.jurisdictions import registry
    from workbench.review import Review

    world = workbench_world.__wrapped__(tmp_path, monkeypatch)
    review_store, library = world
    review = Review(review_store, library, registry())
    approved, waiting = publish.from_workbench(review, library, review_store, "ng")
    assert approved == [] and ACT in waiting  # nothing approved yet
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    confirm_details(approver)
    assert approve(approver).status_code == 200
    approved, waiting = publish.from_workbench(review, library, review_store, "ng")
    assert [a.work for a in approved] == [ACT] and approved[0].approver == "App" and ACT not in waiting


def test_a_link_its_records_no_longer_state_is_withdrawn_and_judicial_links_are_reported(store, decisions):
    from schema.record import Reference, Relation

    def item(work, *relations):
        return publish.Approved(
            work, rwanda_law(relations=relations), ["en"], "Ada Reviewer", datetime.now(UTC)
        )

    def link(kind, work):
        return Relation(kind=kind, target=Reference(citation=f"Instrument {work}", work_uri=work))

    def effects():
        with store.engine.connect() as conn:
            table = namespace(conn).qualified(conn, "changes", "change_event")
            return [
                (r.work_id, r.event_revision, r.payload["legal_effect"].get("review_state"))
                for r in conn.execute(
                    text(
                        f"SELECT * FROM {table} WHERE event_type = 'legal_effect_recorded' ORDER BY work_id, event_revision"
                    )
                )
            ]

    decision = publish.decide("rw", "Ada Reviewer", 12, [], "", "")
    law, treaty = "rw-law-domesticating", "rw-treaty-synthetic"
    approved = [item(law, link("domesticates", treaty), link("follows", "rw-judgment")), item(treaty)]
    report = publish.publish_records(store, "rw", approved, set(), decision)
    assert report["links"] == 1 and report["links_withdrawn"] == []
    [withheld] = report["withheld"]
    assert (withheld["work"], withheld["kind"], withheld["other"]) == (law, "follows", "rw-judgment")
    assert "reviewed citations" in withheld["reason"]  # treatment comes with its paragraph and review
    assert effects() == [(law, 1, None), (treaty, 1, None)]  # one on each work
    # Approved again without the link: it is withdrawn, and each work's event says so once.
    report = publish.publish_records(store, "rw", [item(law), item(treaty)], set(), decision)
    assert report["links_withdrawn"] == [f"link-{law}-domesticates-{treaty}"] and report["withheld"] == []
    assert not next(a for a in store.load().amendments if a.id == report["links_withdrawn"][0]).verified
    assert effects() == [(law, 1, None), (law, 2, "withdrawn"), (treaty, 1, None), (treaty, 2, "withdrawn")]
    assert (
        publish.publish_records(store, "rw", [item(law), item(treaty)], set(), decision)["links_withdrawn"]
        == []
    )
    assert len(effects()) == 4


def test_a_run_publishes_what_the_workbench_approved_and_records_where_each_document_stands(
    store, decisions, tmp_path, monkeypatch
):
    """The publish job's run, end to end: it looks at the documents an approval names, publishes their
    details under the country's decision (no text decision: no text), records the run and each
    document's standing for the workbench, takes up a run asked for there, and takes a document down
    when its approval is revoked."""
    from test_workbench import ACT, approve, confirm_details, flag_id, save, signed_in
    from test_workbench import world as workbench_world  # noqa: F401

    from schema.jurisdictions import registry
    from workbench.review import Review

    world = workbench_world.__wrapped__(tmp_path, monkeypatch)
    review_store, library = world
    review = Review(review_store, library, registry())
    log = publish.PublicationLog(review_store.engine)
    publish.decide("ng", "Ada Reviewer", 12, [], "", "")
    quiet = publish.run(review, library, review_store, store, log)  # nothing approved: nothing to do
    assert quiet["trigger"] == "schedule" and quiet["countries"] == {}
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    # Approved without its official link: the run skips it, records why, and the workbench says so.
    confirm_details(approver)
    assert approve(approver).status_code == 200
    skipped = publish.run(review, library, review_store, store, log)
    assert "no official publication link" in skipped["countries"]["ng"]["records_skipped"][ACT]
    assert review_store.publication_statuses()[ACT]["details"] == "blocked"
    shown = approver.get(f"/api/documents/{ACT}").json()["publication"]
    assert shown["state"] == "blocked" and "no official publication link" in shown["words"]
    # Corrected and approved again: the next run publishes it.
    official = {
        "publication.kind": "gazette",
        "publication.name": "Federal Republic of Nigeria Official Gazette",
        "publication.url": "https://placng.org/i/documents/cybercrimes-2024",
    }
    assert confirm_details(approver, expected=1, **official).status_code == 200
    assert approve(approver, expected=1).status_code == 200
    review_store.request_publication("approver@example.org")
    summary = publish.run(review, library, review_store, store, log)
    assert summary["trigger"] == "manual" and summary["requested_by"] == "approver@example.org"
    assert summary["countries"]["ng"]["records"]["published"] == 1
    assert "No text decision" in summary["countries"]["ng"]["texts"]["blocked"]
    standing = review_store.publication_statuses()[ACT]
    assert (standing["details"], standing["text"]) == ("public", "no_decision") and standing["details_since"]
    assert [r["status"] for r in review_store.publication_runs()] == ["succeeded"] * 3
    client = TestClient(create_app(store=store))
    assert client.get(f"/sources/{ACT}").status_code == 200
    # The workbench says so, on the document and in the list.
    shown = approver.get(f"/api/documents/{ACT}").json()["publication"]
    assert shown["state"] == "public" and shown["text"].startswith("Text not published: No text decision")
    listed = {d["id"]: d for d in approver.get("/api/documents?limit=200").json()["documents"]}
    assert listed[ACT]["publication"] == "public"
    page = approver.get("/api/publishing").json()
    assert [d["id"] for d in page["countries"][0]["documents"]] == [ACT] and len(page["runs"]) == 3
    # Revoked: the next run takes it down, and says so.
    assert approve(approver, expected=2, decision="revoked").status_code == 200
    assert approver.get(f"/api/documents/{ACT}").json()["publication"]["state"] == "taken_down_next"
    after = publish.run(review, library, review_store, store, log)
    assert after["countries"]["ng"]["records"]["taken_down"] == 1
    assert review_store.publication_statuses()[ACT]["details"] == "taken_down"
    assert client.get(f"/sources/{ACT}").status_code == 404


def stored(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT * FROM {table} ORDER BY publication_revision, event_id, event_revision")
            ).mappings()
        ]


def test_every_take_down_a_run_makes_is_in_the_change_log(store, both_decisions, tmp_path):  # noqa: F811
    """A lapsed language, a correction and a new official file, through the publish job's run: what the
    change log records of each, and what reading a text answers afterwards."""
    from test_publish import Collection

    collection = Collection(tmp_path)
    collection.add("rw-law-en", "rw-law", "en")
    collection.add("rw-law-fr", "rw-law", "fr", words="Le Fonds prête.", raw="9" * 64)
    collection.run(store)
    bare = TestClient(create_v1_app(store, tmp_path, lambda: []))
    versions = {v.language: v.id for v in store.load().versions}

    def since(count):
        return [(e["event_type"], e["version_id"]) for e in stored(store)[count:]]

    def readable(version):
        return bare.get(f"/versions/{version}/text").status_code == 200

    # A language lapses, its sibling stays: the text is named as withdrawn, and may be told why.
    count = len(stored(store))
    collection.docs["rw-law-fr"]["approved"] = False
    collection.run(store)
    changes = stored(store)[count:]
    assert ("withdrawal", versions["fr"]) in [(e["event_type"], e["version_id"]) for e in changes]
    [notice] = [e for e in changes if e["event_type"] == "withdrawal"]
    assert notice["payload"]["withdrawal"] == {"action": "stop_serving", "reason_code": "correction"}
    assert readable(versions["en"]) and not readable(versions["fr"])

    # Nothing has changed: nothing is written, nothing announced.
    count = len(stored(store))
    collection.run(store)
    assert since(count) == []

    # A correction of the same file: published, and what it corrects withdrawn, by the same run.
    count = len(stored(store))
    collection.approve("rw-law-en", "The Fund lends.")
    collection.run(store)
    new = next(v for v in store.load().versions if v.prior_version_id == versions["en"])
    changes = stored(store)[count:]
    assert sorted(e["event_type"] for e in changes if e["version_id"]) == [
        "text_version_published",
        "withdrawal",
    ]
    published = next(e for e in changes if e["event_type"] == "text_version_published")
    assert published["change_reason"] == "extraction_correction"
    assert published["payload"]["version_diff"]["prior_version_id"] == versions["en"]
    assert not readable(versions["en"]) and readable(new.id)
    # A citation to what it corrects is not lost, though its words are no longer served.
    node = next(n for n in store.load().structure if n.version_id == versions["en"] and n.locator == "sec_1")
    cited = {"anchor_id": node.id, "span_hash": node.span_hash}
    [answer] = bare.post("/currentness", json={"items": [cited]}).json()["results"]
    assert answer["result"] == "newer_version" and answer["newer_version_id"] == new.id
    assert answer["same_words"] is False and answer["newer_anchor_id"]
    assert "text" not in answer
    wrong = {"anchor_id": node.id, "span_hash": "0" * 64}
    assert (
        bare.post("/currentness", json={"items": [wrong]}).json()["results"][0]["result"] == "span_mismatch"
    )
    # The record reads whole though the text before is gone, asked for any way.
    for query in ("", "?language=en", "?as_of=2026-01-01"):
        page = bare.get(f"/sources/rw-law/versions{query}")
        assert page.status_code == 200, query
    assert [v["id"] for v in bare.get("/sources/rw-law/versions").json()["items"]] == [new.id]
    assert public_corpus(store.load()).versions[0].prior_version_id is None  # a release stands alone

    # A new official file: published beside the earlier file's text, which a citation still reads.
    count = len(stored(store))
    collection.approve("rw-law-en", "The Fund may lend.", raw="8" * 64)
    collection.run(store)
    latest = next(v for v in store.load().versions if v.prior_version_id == new.id)
    changes = stored(store)[count:]
    assert [e["event_type"] for e in changes if e["version_id"]] == ["text_version_published"]
    assert any(e["change_reason"] == "source_revision" for e in changes)
    assert readable(new.id) and readable(latest.id)
    check = bare.post("/currentness", json={"items": [{"version_id": new.id, "locator": "sec_1"}]}).json()
    assert check["results"][0]["result"] == "newer_version"


def test_names_withheld_later_take_the_texts_down_with_nothing_told(store, both_decisions, tmp_path):  # noqa: F811
    from test_publish import Collection

    collection = Collection(tmp_path)
    collection.add("rw-case-en", "rw-case", "en")
    collection.add("rw-case-fr", "rw-case", "fr", words="Le Fonds prête.", raw="9" * 64)
    collection.run(store)
    bare = TestClient(create_v1_app(store, tmp_path, lambda: []))
    count = len(stored(store))
    collection.docs["rw-case-en"]["anonymisation"] = "partial"
    collection.docs["rw-case-fr"]["anonymisation"] = "partial"
    collection.run(store)
    changes = stored(store)[count:]
    assert {e["event_type"] for e in changes} == {"withdrawal"} and len(changes) == 2
    assert all(e["payload"]["withdrawal"]["reason_code"] == "privacy" for e in changes)
    for event in (e for e in bare.get("/changes").json()["events"] if e["event_type"] == "withdrawal"):
        assert event["work"] is None and event["version"] is None
    rights = bare.get("/rights/rw-case").json()
    assert rights["record"]["approved_content_hashes"] == [] and "privacy_status" not in rights["record"]
    assert not any("rivacy" in reason for reason in rights["decision"]["reasons"])
    # Nothing later names it either: the next run has nothing to announce.
    count = len(stored(store))
    collection.run(store)
    assert stored(store)[count:] == []
    with store.engine.begin() as conn:
        from changes.record import sweep

        sweep(conn)
    assert stored(store)[count:] == []


def test_what_else_changed_with_names_withheld_waits_for_a_later_run(store, both_decisions, tmp_path):  # noqa: F811
    """Approved again with its details changed and names withheld: the unnamed withdrawal stands alone, and
    the record's new details come out at a later run, not at the next revision."""
    from test_publish import Collection, example_law

    collection = Collection(tmp_path)
    collection.add("rw-case", "rw-case")
    collection.run(store)
    count = len(stored(store))
    collection.docs["rw-case"].update(anonymisation="partial", record=example_law())
    collection.run(store)
    changes = stored(store)[count:]
    assert [e["event_type"] for e in changes] == ["withdrawal"]
    assert changes[0]["payload"]["withdrawal"]["reason_code"] == "privacy"
    assert next(s for s in store.load().sources if s.id == "rw-case").title.startswith("Law n° 055/2024")
    # The next run: the new title, named, with nothing to tie it to the night before.
    collection.run(store)
    later = stored(store)[count + 1 :]
    assert [e["event_type"] for e in later] == ["metadata_corrected"]
    assert "title" in later[0]["payload"]["changed_fields"]
    assert later[0]["publication_revision"] > changes[0]["publication_revision"]
    assert next(s for s in store.load().sources if s.id == "rw-case").title == "Example Law n° 009/2026"


def test_a_public_title_naming_someone_withheld_leaves_without_being_named(store, both_decisions, tmp_path):  # noqa: F811
    from test_publish import Collection, named_judgment

    collection = Collection(tmp_path)
    collection.add("rw-case", "rw-case", record=named_judgment())
    collection.run(store)
    bare = TestClient(create_v1_app(store, tmp_path, lambda: []))
    assert "Bello" in bare.get("/sources/rw-case").json()["title"]
    count = len(stored(store))
    collection.docs["rw-case"].update(anonymisation="partial", record=named_judgment(True))
    collection.run(store)
    assert bare.get("/sources/rw-case").status_code == 404
    changes = [e for e in stored(store)[count:] if e["event_type"] != "coverage_changed"]
    assert [e["event_type"] for e in changes] == ["withdrawal"] and "withdrawal" not in changes[0]["payload"]
    # The next run it is back, without the name, and nothing public holds the name.
    collection.run(store)
    assert "Bello" not in bare.get("/sources/rw-case").json()["title"]
    assert "Bello" not in bare.get("/changes").text


def test_a_record_whose_names_are_withheld_is_not_named_when_it_comes_down(
    store,
    both_decisions,  # noqa: F811
    tmp_path,
):
    from test_publish import Collection

    collection = Collection(tmp_path)
    collection.add("rw-case", "rw-case")
    collection.run(store)
    # Marked after approval: the approval lapses with its details, so the whole record comes down too.
    collection.docs["rw-case"].update(anonymisation="full", approved=False)
    collection.run(store)
    bare = TestClient(create_v1_app(store, tmp_path, lambda: []))
    assert bare.get("/sources/rw-case").status_code == 404
    withdrawals = [e for e in stored(store) if e["event_type"] == "withdrawal"]
    assert withdrawals and all(
        (e["payload"].get("withdrawal") or {}).get("reason_code") not in ("rights_changed", "correction")
        for e in withdrawals
    )


def test_a_confidential_withdrawal_is_never_undone_by_the_nightly_run(
    store,
    both_decisions,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from test_publish import Collection

    collection = Collection(tmp_path)
    collection.add("rw-law", "rw-law")
    collection.run(store)
    bare = TestClient(create_v1_app(store, tmp_path, lambda: []))
    assert bare.get("/sources/rw-law").status_code == 200
    monkeypatch.setattr(admin, "engine_for", lambda: store.engine)
    assert admin.main(["withdraw-confidentially", "--work", "rw-law"]) == 0
    count = len(stored(store))
    summary = collection.run(store)["countries"]["rw"]
    assert "confidential" in summary["records_skipped"]["rw-law"]
    assert bare.get("/sources/rw-law").status_code == 404 and stored(store)[count:] == []
    assert collection.statuses()["rw-law"]["details"] == "blocked"


def test_an_operator_lifts_a_privacy_block_and_the_next_run_decides(
    store,
    both_decisions,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """The block withdraw-confidentially sets is lifted by its own command, with the same authority. Lifting it
    publishes nothing and tells no one anything: the record stays down, its privacy review pending, until a
    publishing run finds a current approval for it."""
    from test_publish import Collection

    collection = Collection(tmp_path)
    collection.add("rw-law", "rw-law")
    collection.run(store)
    bare = TestClient(create_v1_app(store, tmp_path, lambda: []))
    text_id = bare.get("/sources/rw-law/versions").json()["items"][0]["id"]
    monkeypatch.setattr(admin, "engine_for", lambda: store.engine)
    with pytest.raises(SystemExit):
        admin.main(["lift-privacy-block", "--work", "rw-law"])  # nothing blocks it yet
    assert admin.main(["withdraw-confidentially", "--work", "rw-law"]) == 0
    count = len(stored(store))
    assert admin.main(["lift-privacy-block", "--work", "rw-law"]) == 0
    assert stored(store)[count:] == []
    assert bare.get("/sources/rw-law").status_code == 404
    assert bare.get(f"/versions/{text_id}/text").status_code == 404
    rights = next(r for r in store.load().rights if r.source_id == "rw-law")
    assert rights.privacy_status == "pending" and not rights.metadata_public
    for again in ("rw-law", "rw-no-such-work"):
        with pytest.raises(SystemExit):
            admin.main(["lift-privacy-block", "--work", again])
    assert stored(store)[count:] == []
    # The next run: the approval is current, so the record returns, and its text with it.
    collection.run(store)
    assert bare.get("/sources/rw-law").status_code == 200
    assert bare.get(f"/versions/{text_id}/text").status_code == 200
    assert collection.statuses()["rw-law"]["details"] == "public"
    [restored] = [e for e in stored(store)[count:] if e["event_type"] == "catalogue_added"]
    assert restored["change_reason"] == "restored"
