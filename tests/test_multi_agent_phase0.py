"""Phase 0 (constitution Details first draft, §6) and Phase 5 (status gate helper, §8)."""

import json

import pytest
from fastapi.testclient import TestClient

from schema.jurisdictions import registry
from workbench import content, record_view, review
from workbench.app import create_app
from workbench.store import Store, connect


def _simple_reading(text: str = "Article 1. The Republic of Rwanda is an independent State.") -> dict:
    page = {
        "page": 1,
        "nodes": [{"kind": "paragraph", "level": "paragraph", "text": text, "page": 1}],
        "text": text,
        "tables": [],
        "furniture": [],
        "flags": [],
        "checks": {"mineru": "same", "azure": "same"},
    }
    return {
        "input_sha256": "a" * 64,
        "primary": {"reader": "docai", "version": {"version": "test"}},
        "checks": {"mineru": {}, "azure": {}},
        "summary": {
            "pages": 1,
            "flags": {"high": 0, "medium": 0, "low": 0},
            "restored_markers": 0,
            "missing_checks": [],
        },
        "pages": [page],
    }


def _rwanda_constitution_doc(act_id: str = "rw-constitution-2023-en") -> dict:
    return {
        "id": act_id,
        "work": "rw-constitution-2023",
        "jurisdiction": "rw",
        "language": "en",
        "type": "constitution",
        "title": "The Constitution of the Republic of Rwanda",
        "year": 2023,
        "suggested": {
            "identity.family": "constitution",
            "identity.type": "constitution",
            "identity.title": "The Constitution of the Republic of Rwanda",
            "identity.full_title": "The Constitution of the Republic of Rwanda of 2003 revised in 2015 and 2023",
            "identity.number.value": "2023",
            "identity.citation": "Constitution of the Republic of Rwanda (2023 revision)",
            "issued_by": "Parliament of the Republic of Rwanda",
            "publication.name": "Official Gazette of the Republic of Rwanda",
            "publication.number": "Special",
            "publication.date": "2023-08-04",
            "text.form": "gazette_publication",
            "expressions": [
                {"language": "rw", "title": "Itegeko Nshinga rya Repubulika y'u Rwanda"},
                {"language": "en", "title": "The Constitution of the Republic of Rwanda"},
                {"language": "fr", "title": "Constitution de la République du Rwanda"},
            ],
            # Steps Rwanda's gazette reader emits that Rwanda's constitution form does not show:
            "step:promulgated.date": "2023-08-04",
            "step:promulgated.person": "KAGAME Paul, President of the Republic",
            "step:countersigned.person": "Dr. NGIRENTE Edouard, Prime Minister",
            "step:sealed.person": "Dr. UGIRASHEBUJA Emmanuel, Minister of Justice and Attorney General",
            "step:published.date": "2023-08-04",
        },
        "suggested_evidence": {
            "step:promulgated.date": {
                "value": "2023-08-04",
                "page": 1,
                "evidence": "Kigali, on 04/08/2023",
            }
        },
    }


def _make_world(tmp_path, monkeypatch, docs: list[dict], jurisdiction: str = "rw"):
    manifest = {
        "jurisdiction": jurisdiction,
        "description": "Test collection",
        "rights_basis": "Official publication in the public domain",
        "documents": [
            {
                "id": d["id"],
                "work": d.get("work", d["id"]),
                "jurisdiction": d.get("jurisdiction", jurisdiction),
                "language": d.get("language", "en"),
                "type": d.get("type", "law"),
                "title": d.get("title", d["id"]),
                "year": d.get("year", 2023),
                "pages": 1,
            }
            for d in docs
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    for d in docs:
        (tmp_path / f"{d['id']}.reading.json").write_text(json.dumps(_simple_reading()))
        (tmp_path / f"{d['id']}.document.json").write_text(json.dumps(d))
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(tmp_path))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    store.add_operator("approver@example.org", "App", {"reviewer", "approver"})
    library = content.Library(tmp_path)
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(library, "image", lambda act_id, page: image)
    return store, library


def _signed_in(world, email: str) -> TestClient:
    store, library = world
    browser = TestClient(create_app(store, library, dev_login=True), base_url="http://127.0.0.1")
    assert browser.post("/auth/dev", json={"email": email}).status_code == 200
    browser.headers["X-CSRF-Token"] = browser.get("/api/me").json()["csrf"]
    return browser


def _check_page_one(browser: TestClient, act_id: str) -> None:
    response = browser.post(
        f"/api/documents/{act_id}/pages/1",
        json={
            "expected_revision": 0,
            "units": [
                {
                    "kind": "paragraph",
                    "text": "Article 1. The Republic of Rwanda is an independent State.",
                }
            ],
            "status": "checked",
            "resolutions": {},
        },
    )
    assert response.status_code == 200, response.text


def _approve_doc(browser: TestClient, act_id: str):
    detail = browser.get(f"/api/documents/{act_id}").json()
    materials = {k: True for k in detail["materials"]}
    shown = {
        "content_sha256": detail["content_sha256"],
        "details_sha256": detail["details_sha256"],
        "text_sha256": detail["text_check"]["text_sha256"],
    }
    return browser.post(
        f"/api/documents/{act_id}/approval",
        json={
            "expected_revision": len(detail["approvals"]),
            "decision": "approved",
            "rights_basis": "Official publication in the public domain",
            "materials": materials,
            "shown": shown,
        },
    )


# --- Phase 0: Constitution Details first draft (§6) --------------------------------------------------


def test_constitution_form_keeps_its_own_keys_and_names_what_it_has_no_field_for():
    rwanda = registry()["rw"]
    doc = _rwanda_constitution_doc()
    steps = {
        "step:promulgated.date",
        "step:promulgated.person",
        "step:countersigned.person",
        "step:sealed.person",
        "step:published.date",
    }

    # The first draft keeps what the reader read, whatever the form: other code counts on it
    # (details_audit reports on a first draft's signing steps).
    draft = record_view.base_values(doc, {})
    assert steps <= set(draft)

    # The form holds only keys it has fields for, and what it leaves out is named, never dropped in silence.
    specs = record_view.fields("constitution", "constitution", rwanda)
    values, absent, left_out = record_view.for_form(draft, {}, specs)
    assert set(values) <= {spec.key for spec in specs}
    assert set(left_out) == steps
    notice = record_view.left_out_notice(left_out, rwanda, doc.get("suggested_evidence"))
    assert notice is not None
    assert notice.startswith("The reader also read these, which this form has no field for:")
    for words in ("KAGAME Paul", "Dr. NGIRENTE Edouard", "Dr. UGIRASHEBUJA Emmanuel"):
        assert words in notice

    # What is left validates with no "unknown details" refusal.
    clean, still_absent = record_view.validate(values, absent, specs)
    assert clean["identity.family"] == "constitution"
    assert still_absent == {}


def test_organic_law_keeps_rwanda_making_steps_in_first_draft():
    rwanda = registry()["rw"]
    law = {
        "id": "rw-ol-001-2023-en",
        "jurisdiction": "rw",
        "type": "organic_law",
        "title": "Organic Law n° 001/2023.OL",
        "suggested": {
            "identity.family": "legislation",
            "identity.type": "organic_law",
            "identity.title": "Organic Law n° 001/2023.OL",
            "step:promulgated.date": "2023-08-04",
            "step:promulgated.person": "KAGAME Paul, President of the Republic",
            "step:countersigned.person": "Dr. NGIRENTE Edouard, Prime Minister",
            "step:sealed.person": "Dr. UGIRASHEBUJA Emmanuel, Minister of Justice and Attorney General",
            "step:published.date": "2023-08-04",
        },
    }
    values = record_view.base_values(law, {})
    assert values["step:promulgated.date"] == "2023-08-04"
    assert values["step:countersigned.person"] == "Dr. NGIRENTE Edouard, Prime Minister"
    assert (
        values["step:sealed.person"] == "Dr. UGIRASHEBUJA Emmanuel, Minister of Justice and Attorney General"
    )
    # A law's form has the signing steps, so nothing is left out of it.
    specs = record_view.fields("legislation", "organic_law", rwanda)
    assert record_view.for_form(values, {}, specs)[2] == {}


def test_details_audit_still_sees_a_first_draft_signing_steps():
    """The first-draft report read by ingestion (details_audit) calls base_values with no profile: the
    steps the reader read must be in it, or every law is reported as missing them."""
    law = {
        "id": "rw-ol-001-2023-en",
        "jurisdiction": "rw",
        "type": "organic_law",
        "title": "Organic Law n° 001/2023.OL",
        "suggested": {
            "identity.family": "legislation",
            "identity.type": "organic_law",
            "identity.title": "Organic Law n° 001/2023.OL",
            "step:promulgated.date": "2023-08-04",
            "step:published.date": "2023-08-04",
        },
    }
    values = record_view.base_values(law, {})
    assert values["step:promulgated.date"] == "2023-08-04"
    assert values["step:published.date"] == "2023-08-04"


def test_constitution_first_draft_saves_and_confirms_end_to_end(tmp_path, monkeypatch):
    doc = _rwanda_constitution_doc()
    world = _make_world(tmp_path, monkeypatch, [doc], jurisdiction="rw")
    browser = _signed_in(world, "reviewer@example.org")

    details_resp = browser.get(f"/api/documents/{doc['id']}/details")
    assert details_resp.status_code == 200
    payload = details_resp.json()
    draft_values = payload["record"]["details"]
    draft_absent = payload["record"]["absent"]

    # No unknown step:* keys leaked into the first draft.
    assert not any(k.startswith("step:") for k in draft_values)
    # The dropped values are shown in the Details page notices.
    assert any(
        "The reader also read these, which this form has no field for:" in n
        and "KAGAME Paul" in n
        and "Dr. NGIRENTE Edouard" in n
        for n in payload["notices"]
    )

    # Saving the first draft directly succeeds (never fails with "unknown details").
    saved = browser.post(
        f"/api/documents/{doc['id']}/details",
        json={
            "expected_revision": 0,
            "status": "draft",
            "details": draft_values,
            "absent": draft_absent,
            "comment": "First draft save",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1

    # Confirming the first draft (accounting for required constitution events) succeeds.
    confirmed_values = {
        **draft_values,
        "event:adopted.date": "2023-08-04",
        "event:commenced.date": "2023-08-04",
    }
    confirmed = browser.post(
        f"/api/documents/{doc['id']}/details",
        json={
            "expected_revision": 1,
            "status": "confirmed",
            "details": confirmed_values,
            "absent": draft_absent,
            "comment": "Confirmed constitution details",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["revision"] == 2

    # The strict write check still refuses unknown keys if submitted explicitly.
    rejected = browser.post(
        f"/api/documents/{doc['id']}/details",
        json={
            "expected_revision": 2,
            "status": "draft",
            "details": {**confirmed_values, "step:promulgated.date": "2023-08-04"},
            "absent": draft_absent,
        },
    )
    assert rejected.status_code == 422
    assert "unknown details" in rejected.text


# --- Phase 5: Status gate helper (§8) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "family",
    [
        "constitution",
        "legislation",
        "subsidiary_legislation",
        "executive_instrument",
        "treaty",
        "regional_instrument",
    ],
)
def test_status_research_pending_true_for_eligible_families_with_sentinel(family):
    record = {
        "status": "confirmed",
        "details": {"identity.family": family, "status.legal": "unknown", "status.note": ""},
        "absent": {},
    }
    assert review.status_research_pending(record, {"type": "law"}) is True
    # Whitespace-only or omitted status.note / status.legal also counts as pending.
    record_ws = {
        "status": "confirmed",
        "details": {"identity.family": family, "status.note": "   "},
        "absent": {},
    }
    assert review.status_research_pending(record_ws, {}) is True
    # Writing what sources answered in status.note ends the pending state.
    record_noted = {
        "status": "confirmed",
        "details": {
            "identity.family": family,
            "status.legal": "unknown",
            "status.note": "Checked Official Gazette and RLRC; no repeal found.",
        },
        "absent": {},
    }
    assert review.status_research_pending(record_noted, {}) is False
    # Settling status.legal also ends the pending state.
    record_settled = {
        "status": "confirmed",
        "details": {"identity.family": family, "status.legal": "in_force", "status.note": ""},
        "absent": {},
    }
    assert review.status_research_pending(record_settled, {}) is False
    # Draft details are never pending (status research waits for confirmed Details).
    assert review.status_research_pending({**record, "status": "draft"}, {}) is False


@pytest.mark.parametrize(
    "family",
    [
        "judgment",
        "gazette",
        "gazette_notice",
        "bill",
        "soft_law",
        "parliamentary_record",
        "official_document",
    ],
)
def test_status_research_pending_false_for_ineligible_families(family):
    record = {
        "status": "confirmed",
        "details": {"identity.family": family, "status.legal": "unknown", "status.note": ""},
        "absent": {},
    }
    assert review.status_research_pending(record, {}) is False
    assert review.status_research_pending(None, {}) is False


def _confirmed_law_details(status_legal: str = "unknown", status_note: str = "") -> tuple[dict, dict]:
    values = {
        "identity.family": "legislation",
        "identity.type": "law",
        "identity.title": "Law n° 010/2023 on examples",
        "identity.full_title": "Law n° 010/2023 of 01/03/2023 on examples",
        "identity.number.value": "010/2023",
        "identity.year": "2023",
        "identity.citation": "Law n° 010/2023 of 01/03/2023",
        "issued_by": "Parliament of the Republic of Rwanda",
        "step:adopted:deputies.date": "2023-02-01",
        "step:adopted:senate.date": "2023-02-10",
        "step:promulgated.date": "2023-03-01",
        "step:countersigned.date": "2023-03-01",
        "step:sealed.date": "2023-03-01",
        "step:published.date": "2023-03-01",
        "step:commenced.date": "2023-03-01",
        "publication.name": "Official Gazette of the Republic of Rwanda",
        "text.form": "gazette_publication",
        "text.version.label": "As enacted",
        "status.legal": status_legal,
        "expressions": [{"language": "en", "this_text": "yes"}],
        "classification.hierarchy": "primary",
    }
    if status_note:
        values["status.note"] = status_note
    absent = {"profile.bill": "not_in_source", "profile.revised_edition": "not_applicable"}
    return values, absent


def _confirmed_treaty_details(status_legal: str = "unknown", status_note: str = "") -> tuple[dict, dict]:
    values = {
        "identity.family": "treaty",
        "identity.type": "treaty",
        "identity.title": "Example Regional Treaty",
        "identity.full_title": "Example Regional Treaty of 2023",
        "identity.number.value": "2023-01",
        "identity.year": "2023",
        "identity.citation": "Example Regional Treaty (2023)",
        "issued_by": "African Union",
        "event:adopted.date": "2023-05-01",
        "event:entered_into_force.date": "2023-06-01",
        "publication.name": "Official Gazette of the Republic of Rwanda",
        "text.form": "gazette_publication",
        "text.version.label": "As published",
        "status.legal": status_legal,
        "expressions": [{"language": "en", "this_text": "yes"}],
        "classification.hierarchy": "international",
        "profile.parties": [{"party": "Rwanda", "place": "rw", "ratified": "2023-05-15"}],
        "profile.depositary": "African Union Commission",
    }
    if status_note:
        values["status.note"] = status_note
    return values, {}


def _confirmed_judgment_details(status_legal: str = "unknown", status_note: str = "") -> tuple[dict, dict]:
    values = {
        "identity.family": "judgment",
        "identity.type": "judgment",
        "identity.title": "Alpha v. Beta",
        "identity.full_title": "Alpha v. Beta (Supreme Court)",
        "identity.number.value": "RS/INCONST/001/2023",
        "identity.year": "2023",
        "identity.citation": "RS/INCONST/001/2023",
        "issued_by": "Supreme Court of Rwanda",
        "event:decided.date": "2023-07-01",
        "publication.name": "Judiciary of Rwanda",
        "text.form": "court_copy",
        "text.version.label": "As published",
        "status.legal": status_legal,
        "expressions": [{"language": "en", "this_text": "yes"}],
        "classification.hierarchy": "judicial",
        "profile.court.name": "Supreme Court",
        "profile.court.level": "apex",
        "profile.case_numbers": ["RS/INCONST/001/2023"],
        "profile.parties": [{"name": "Alpha", "role": "appellant"}, {"name": "Beta", "role": "respondent"}],
        "profile.bench": [{"name": "Justice One", "role": "presiding"}],
    }
    if status_note:
        values["status.note"] = status_note
    return values, {}


def test_status_gate_for_laws_treaties_judgments_and_already_approved(tmp_path, monkeypatch):
    law_doc = {"id": "rw-law-010-2023-en", "work": "rw-law-010-2023", "type": "law", "title": "Law 010/2023"}
    treaty_doc = {"id": "rw-treaty-2023-en", "work": "rw-treaty-2023", "type": "treaty", "title": "Treaty"}
    judgment_doc = {
        "id": "ng-judgment-2023-en",
        "work": "ng-judgment-2023",
        "jurisdiction": "ng",
        "type": "judgment",
        "title": "Alpha v. Beta",
    }
    approved_law_doc = {
        "id": "rw-law-011-2023-en",
        "work": "rw-law-011-2023",
        "type": "law",
        "title": "Law 011/2023 (approved)",
    }
    world = _make_world(tmp_path, monkeypatch, [law_doc, treaty_doc, judgment_doc, approved_law_doc])
    store, library = world
    rev = review.Review(store, library, registry())
    approver = _signed_in(world, "approver@example.org")

    # Prepare all four documents: page 1 checked + confirmed Details with status.legal="unknown" & empty note.
    for doc, (vals, absent) in (
        (law_doc, _confirmed_law_details()),
        (treaty_doc, _confirmed_treaty_details()),
        (judgment_doc, _confirmed_judgment_details()),
        (approved_law_doc, _confirmed_law_details()),
    ):
        _check_page_one(approver, doc["id"])
        r = approver.post(
            f"/api/documents/{doc['id']}/details",
            json={"expected_revision": 0, "status": "confirmed", "details": vals, "absent": absent},
        )
        assert r.status_code == 200, r.text

    # 1. With ALVARY_REVIEW_STATUS_GATE off (default), all four are ready; approve approved_law_doc now.
    monkeypatch.delenv("ALVARY_REVIEW_STATUS_GATE", raising=False)
    for doc in (law_doc, treaty_doc, judgment_doc, approved_law_doc):
        st = rev.state(library.act(doc["id"]), text=True)
        assert st["readiness"]["ready"] is True, (doc["id"], st["readiness"])
        assert st["readiness"]["status_pending"] is False
    assert _approve_doc(approver, approved_law_doc["id"]).status_code == 200

    # 2. Switch ALVARY_REVIEW_STATUS_GATE=on:
    monkeypatch.setenv("ALVARY_REVIEW_STATUS_GATE", "on")

    # Law: not ready, surfaces exact message, approval refused with 409.
    law_state = rev.state(library.act(law_doc["id"]), text=True)
    assert law_state["readiness"]["ready"] is False
    assert law_state["readiness"]["status_pending"] is True
    assert law_state["readiness"]["status_pending_message"] == "Legal status: research pending."
    refused_law = _approve_doc(approver, law_doc["id"])
    assert refused_law.status_code == 409
    assert refused_law.json()["readiness"]["status_pending_message"] == "Legal status: research pending."

    # Treaty: also gated.
    treaty_state = rev.state(library.act(treaty_doc["id"]), text=True)
    assert treaty_state["readiness"]["ready"] is False
    assert treaty_state["readiness"]["status_pending"] is True
    assert treaty_state["readiness"]["status_pending_message"] == "Legal status: research pending."
    assert _approve_doc(approver, treaty_doc["id"]).status_code == 409

    # Judgment: never gated, even with status.legal == "unknown" and empty status.note.
    judgment_state = rev.state(library.act(judgment_doc["id"]), text=True)
    assert judgment_state["readiness"]["ready"] is True
    assert judgment_state["readiness"]["status_pending"] is False
    assert _approve_doc(approver, judgment_doc["id"]).status_code == 200

    # Already-approved law: approved stays True (never taken down by enabling the gate!).
    approved_state = rev.state(library.act(approved_law_doc["id"]), text=True)
    assert approved_state["approved"] is True
    assert approved_state["readiness"]["status_pending"] is True
    assert approved_state["readiness"]["ready"] is False

    # Completing status research (writing status.note on law, or settling status.legal on treaty) unblocks.
    law_vals, law_absent = _confirmed_law_details(
        status_legal="unknown",
        status_note="Checked RLRC and Official Gazette; no repeal or amendment found.",
    )
    assert (
        approver.post(
            f"/api/documents/{law_doc['id']}/details",
            json={"expected_revision": 1, "status": "confirmed", "details": law_vals, "absent": law_absent},
        ).status_code
        == 200
    )
    assert rev.state(library.act(law_doc["id"]), text=True)["readiness"]["ready"] is True
    assert _approve_doc(approver, law_doc["id"]).status_code == 200

    treaty_vals, treaty_absent = _confirmed_treaty_details(status_legal="in_force")
    assert (
        approver.post(
            f"/api/documents/{treaty_doc['id']}/details",
            json={
                "expected_revision": 1,
                "status": "confirmed",
                "details": treaty_vals,
                "absent": treaty_absent,
            },
        ).status_code
        == 200
    )
    assert rev.state(library.act(treaty_doc["id"]), text=True)["readiness"]["ready"] is True
    assert _approve_doc(approver, treaty_doc["id"]).status_code == 200
