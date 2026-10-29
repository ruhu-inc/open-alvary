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
