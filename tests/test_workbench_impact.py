"""What preparing the collections again would do to review work already done (workbench.impact; owner,
2026-10-30): pages whose reading is the same keep their check, changed pages are to check again, a
document no longer in the new copy is named with where its pages are now, and approvals that would lapse
are listed, all before the workbench is switched to the new copy."""

import json

from test_workbench import ACT, reading

from schema.jurisdictions import registry
from workbench import content, impact
from workbench.store import Store, connect


def test_review_work_against_a_newly_prepared_copy(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    before = reading()
    (old / f"{ACT}.reading.json").write_text(json.dumps(before))
    after = json.loads(json.dumps(before))
    after["pages"][1]["nodes"][0]["text"] = "2. Short title."  # the reader now reads page 2 otherwise
    (new / f"{ACT}.reading.json").write_text(json.dumps(after))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    for page in before["pages"]:
        store.save_page(
            ACT,
            page["page"],
            0,
            "rev@example.org",
            reading_sha256=content.reading_sha256(page),
            units=[],
            status="checked",
            resolutions={},
            comment="",
        )
    found = impact.report(store, content.Library(old), content.Library(new), registry())
    assert found["summary"]["pages unchanged (their check stands)"] == 1
    assert found["summary"]["pages changed (check again)"] == 1
    assert found["changed"] == [{"id": ACT, "pages_to_check_again": [2], "pages_no_longer_here": []}]
    gone = impact.report(store, content.Library(old), content.Library(tmp_path / "empty"), registry())
    assert (
        gone["documents_gone"][0]["id"] == ACT and gone["summary"]["documents no longer in the new copy"] == 1
    )


def test_a_judgment_without_a_neutral_citation_says_so_in_its_first_draft():
    from workbench import record_view as V

    assert V.first_absent({"identity.family": "judgment"}) == {"identity.citation": "not_in_source"}
    assert V.first_absent({"identity.family": "judgment", "identity.citation": "[2026] NICN 1"}) == {}
    assert V.first_absent({"identity.family": "legislation"}) == {}


def test_a_document_found_in_another_file_is_named_as_where_its_pages_are_now():
    old = {"id": "rw-instructions-2012-b5acd9-en", "url": "https://example.org/rlrc.pdf", "language": "en"}

    class Library:
        def documents(self):
            return [
                {
                    "id": "rw-instructions-002-03-2012-en",
                    "url": "https://example.org/gazette.pdf",
                    "language": "en",
                    "pages": [66, 70],
                    "other_sources": [{"url": "https://example.org/rlrc.pdf"}],
                },
                {
                    "id": "rw-instructions-002-03-2012-fr",
                    "url": "https://example.org/gazette.pdf",
                    "language": "fr",
                },
            ]

    assert impact.successors(old, Library(), {1, 2}) == ["rw-instructions-002-03-2012-en"]


def test_a_renamed_documents_checks_carry_over_only_where_its_pages_read_the_same(tmp_path):
    """2026-11-01: RURA's regulation, its number read whole, took a new id; the 22 pages its reviewer had
    checked under the old one were carried over where they read exactly the same."""
    new = tmp_path / "new"
    new.mkdir()
    before = reading()
    after = json.loads(json.dumps(before))
    after["pages"][1]["nodes"][0]["text"] = "2. Short title."  # page 2 reads otherwise now
    (new / f"{ACT}.reading.json").write_text(json.dumps(after))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    old = "ng-renamed-act"
    for page in before["pages"]:
        store.save_page(old, page["page"], 0, "rev@example.org", reading_sha256=content.reading_sha256(page),
                        units=[], status="checked", resolutions={}, comment="")  # fmt: skip
    listed = impact.carry(store, content.Library(new), old, ACT)
    assert listed == {"carried": [1], "changed": [2], "not_in_document": [], "already_checked": []}
    assert store.latest_pages(ACT) == {}  # nothing written without --apply
    impact.carry(store, content.Library(new), old, ACT, apply=True)
    (carried,) = store.latest_pages(ACT).values()
    assert carried["page"] == 1 and carried["author"] == "rev@example.org" and old in carried["comment"]
    assert impact.carry(store, content.Library(new), old, ACT)["already_checked"] == [1]


def test_a_check_is_kept_where_only_a_pages_flags_changed_and_none_serious_is_new(tmp_path):
    """2026-11-01: the comparison of readings stopped flagging words both readings have elsewhere on the
    page; checked pages whose text had not changed were to be checked again for their flags alone."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    before = reading()
    third = {**before["pages"][1], "page": 3, "nodes": [{**before["pages"][1]["nodes"][0], "page": 3}]}
    before["pages"].append(third)
    (old / f"{ACT}.reading.json").write_text(json.dumps(before))
    after = json.loads(json.dumps(before))
    after["pages"][0]["flags"] = [{"severity": "low", "kind": "moved", "primary": "198", "tokens": [4, 5]}]
    after["pages"][1]["flags"] = [
        {"severity": "medium", "kind": "differs", "primary": "Citation", "tokens": [1, 2]}
    ]
    after["pages"][2]["nodes"][0]["text"] = "3. Commencement."
    after["pages"][2]["flags"] = []
    (new / f"{ACT}.reading.json").write_text(json.dumps(after))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    (high,) = content.page_flags(before["pages"][0])
    for page in before["pages"]:
        resolved = (
            {high["id"]: {"decision": "primary", "reader": "", "note": ""}} if page["page"] == 1 else {}
        )
        store.save_page(ACT, page["page"], 0, "rev@example.org", reading_sha256=content.reading_sha256(page),
                        units=[], status="checked", resolutions=resolved, comment="")  # fmt: skip
    previous, library = content.Library(old), content.Library(new)
    listed = impact.carry_unchanged(store, previous, library)
    assert listed == {"carried": {ACT: [1]}, "new_flags": {ACT: [2]}, "text_changed": {ACT: [3]},
                      "not_as_checked": {}}  # fmt: skip
    assert store.latest_pages(ACT)[1]["revision"] == 1  # nothing written without --apply
    impact.carry_unchanged(store, previous, library, apply=True)
    kept = store.latest_pages(ACT)[1]
    assert kept["revision"] == 2 and kept["author"] == "rev@example.org" and kept["resolutions"] == {}
    assert kept["reading_sha256"] == content.reading_sha256(after["pages"][0])
    assert impact.carry_unchanged(store, previous, library)["carried"] == {}  # its check stands now
