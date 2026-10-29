import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from workbench import auth, content, details, record_view
from workbench.app import create_app
from workbench.store import Store, connect, session

ACT = "ng-cybercrimes-amendment-act-2024"
FLAG = {
    "severity": "high",
    "kind": "differs",
    "primary": "198",
    "checks": {"mineru": "199", "azure": "199"},
    "tokens": [4, 5],
    "readers_disagreeing": ["azure", "mineru"],
}


def node(text, kind="paragraph", **extra):
    return {"kind": kind, "level": kind, "text": text, "page": 1, **extra}


def reading():
    page_one = {
        "page": 1,
        "nodes": [node("1. Accounts prepared under section 198 of this Act.", notes=["Accounts"])],
        "text": "1. Accounts prepared under section 198 of this Act.\nAccounts",
        "tables": [],
        "furniture": [],
        "flags": [FLAG],
        "checks": {"mineru": "1. Accounts prepared under section 199 of this Act.", "azure": "same"},
    }
    page_two = {
        **page_one,
        "page": 2,
        "nodes": [node("2. Citation.", page=2)],
        "flags": [],
        "text": "2. Citation.",
    }
    return {
        "input_sha256": "0" * 64,
        "primary": {
            "reader": "docai",
            "version": {"version": "pretrained-layout-parser-v1.6-pro-2025-12-01"},
        },
        "checks": {"mineru": {}, "azure": {}},
        "summary": {
            "pages": 2,
            "flags": {"high": 1, "medium": 0, "low": 0},
            "restored_markers": 0,
            "missing_checks": [],
        },
        "pages": [page_one, page_two],
    }


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    store.add_operator("approver@example.org", "App", {"reviewer", "approver"})
    library = content.Library(tmp_path)
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(library, "image", lambda act_id, page: image)
    return store, library


def client(world, oidc=None, dev=True, host="127.0.0.1", scheme="http"):
    store, library = world
    return TestClient(create_app(store, library, oidc, dev_login=dev), base_url=f"{scheme}://{host}")


def signed_in(world, email):
    browser = client(world)
    assert browser.post("/auth/dev", json={"email": email}).status_code == 200
    browser.headers["X-CSRF-Token"] = browser.get("/api/me").json()["csrf"]
    return browser


def save(browser, page=1, expected=0, units=None, status="checked", resolutions=None):
    units = units or [
        {
            "kind": "paragraph",
            "text": "1. Accounts prepared under section 199 of this Act.",
            "notes": ["Accounts"],
        }
    ]
    return browser.post(
        f"/api/documents/{ACT}/pages/{page}",
        json={
            "expected_revision": expected,
            "units": units,
            "status": status,
            "resolutions": resolutions or {},
        },
    )


def flag_id():
    return content.flag_id(1, FLAG)


# --- access ------------------------------------------------------------------------------------------


def test_everything_needs_a_signed_in_operator_and_sends_strict_headers(world):
    browser = client(world, dev=False)
    me = browser.get("/api/me")
    assert me.status_code == 401 and me.json() == {"signed_in": False, "oidc": False, "dev_login": False}
    for path in (
        "/api/documents",
        f"/api/documents/{ACT}",
        f"/api/documents/{ACT}/pages/1",
        f"/api/documents/{ACT}/pages/1/image",
        f"/api/documents/{ACT}/export",
        f"/api/documents/{ACT}/public-preview",
    ):
        assert browser.get(path).status_code == 401
    page = browser.get("/")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


def test_trial_sign_in_is_off_by_default_and_answers_only_on_loopback(world):
    assert (
        client(world, dev=False).post("/auth/dev", json={"email": "reviewer@example.org"}).status_code == 404
    )
    assert (
        client(world, host="review.example.org")
        .post("/auth/dev", json={"email": "reviewer@example.org"})
        .status_code
        == 404
    )
    assert client(world).post("/auth/dev", json={"email": "stranger@example.org"}).status_code == 403


def test_writes_need_the_csrf_token_and_the_right_role(world):
    reviewer = signed_in(world, "reviewer@example.org")
    del reviewer.headers["X-CSRF-Token"]
    assert save(reviewer).status_code == 403
    reviewer = signed_in(world, "reviewer@example.org")
    assert save(reviewer).status_code == 200
    refused = reviewer.post(
        f"/api/documents/{ACT}/approval", json={"expected_revision": 0, "decision": "approved"}
    )
    assert refused.status_code == 403 and "approver" in refused.json()["message"]


def test_a_deactivated_operator_and_a_signed_out_session_lose_access(world):
    store, _ = world
    browser = signed_in(world, "reviewer@example.org")
    store.deactivate("reviewer@example.org")
    assert browser.get("/api/documents").status_code == 401
    other = signed_in(world, "approver@example.org")
    assert other.post("/auth/logout").status_code == 200
    assert other.get("/api/documents").status_code == 401


def test_an_idle_session_expires(world):
    store, _ = world
    browser = signed_in(world, "reviewer@example.org")
    with store.engine.begin() as db:
        db.execute(update(session).values(last_seen=store_now_minus(hours=2)))
    assert browser.get("/api/documents").status_code == 401


def store_now_minus(hours):
    from datetime import timedelta

    from workbench.store import now

    return now() - timedelta(hours=hours)


# --- review ------------------------------------------------------------------------------------------


def test_page_detail_offers_units_flags_and_image(world):
    browser = signed_in(world, "reviewer@example.org")
    page = browser.get(f"/api/documents/{ACT}/pages/1").json()
    assert page["machine_units"] == [
        {
            "kind": "paragraph",
            "text": "1. Accounts prepared under section 198 of this Act.",
            "notes": ["Accounts"],
        }
    ]
    assert page["flags"][0]["id"] == flag_id() and page["latest"] is None
    assert browser.get(f"/api/documents/{ACT}/pages/1/image").headers["content-type"] == "image/jpeg"
    assert browser.get(f"/api/documents/{ACT}/pages/9").status_code == 404
    assert browser.get("/api/documents/ng-unknown/pages/1").status_code == 404


def test_a_document_says_where_to_check_it_against_its_source(world, tmp_path):
    browser = signed_in(world, "reviewer@example.org")
    (tmp_path / f"{ACT}.provenance.json").write_text(
        json.dumps({"url": "https://www.example.gov.ng/act.pdf"})
    )
    source = browser.get(f"/api/documents/{ACT}").json()["source"]
    assert source == {"official_url": "https://www.example.gov.ng/act.pdf", "original_pdf": False}
    (tmp_path / f"{ACT}.pdf").write_bytes(b"%PDF-1.4\n")
    assert browser.get(f"/api/documents/{ACT}").json()["source"]["original_pdf"] is True
    assert browser.get(f"/api/documents/{ACT}/original.pdf").content == b"%PDF-1.4\n"


def test_a_deployed_collection_shows_its_prepared_page_images_without_the_pdf(tmp_path, monkeypatch):
    # The workbench upload carries readings and page images, never PDFs (deployment/gcp/workbench.sh).
    monkeypatch.delenv("ALVARY_REVIEW_COLLECTIONS", raising=False)
    monkeypatch.setenv("ALVARY_RENDER", "direct")
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    folder = tmp_path / "review-images" / ACT
    folder.mkdir(parents=True)
    (folder / "page-1.jpg").write_bytes(b"\xff\xd8 prepared")
    library = content.Library(tmp_path)
    # The deployed workbench mounts the collection read-only: its images are served, never touched.
    monkeypatch.setattr(
        content.os, "utime", lambda path: (_ for _ in ()).throw(OSError(30, "Read-only file system"))
    )
    assert library.image(ACT, 1).read_bytes() == b"\xff\xd8 prepared"
    with pytest.raises(FileNotFoundError):
        library.image(ACT, 2)  # neither a prepared image nor a PDF to render one from


def test_saving_is_versioned_and_a_stale_save_conflicts(world):
    first = signed_in(world, "reviewer@example.org")
    second = signed_in(world, "approver@example.org")
    assert save(first).json() == {"revision": 1}
    conflict = save(second, expected=0)
    assert conflict.status_code == 409
    assert save(second, expected=1, status="attention").json() == {"revision": 2}
    history = first.get(f"/api/documents/{ACT}/pages/1").json()["history"]
    assert [(h["revision"], h["author"]) for h in history] == [
        (2, "approver@example.org"),
        (1, "reviewer@example.org"),
    ]


def test_unit_shapes_and_flag_resolutions_are_validated(world):
    browser = signed_in(world, "reviewer@example.org")
    assert save(browser, units=[{"kind": "script", "text": "x"}]).status_code == 422
    assert save(browser, units=[{"kind": "paragraph", "text": "x" * 9000}]).status_code == 422
    assert save(browser, resolutions={"nope": {"decision": "keep"}}).status_code == 422
    assert save(browser, resolutions={flag_id(): {"decision": "approve-anyway"}}).status_code == 422
    table = [{"kind": "table", "caption": "Schedule", "header_rows": 1, "rows": [["A", "B"], ["1", "2"]]}]
    assert save(browser, units=table).status_code == 200


# --- approval ----------------------------------------------------------------------------------------

MATERIALS = dict.fromkeys(content.MATERIALS, True)


def test_a_judgment_is_verified_in_its_own_words_under_the_same_keys():
    worded = content.materials_for({"type": "ruling"})
    assert set(worded) == set(content.MATERIALS) and "suit number" in worded["identity_citation"]
    assert content.materials_for({"type": "law"}) is content.MATERIALS


def approve(browser, expected=0, **overrides):
    body = {
        "expected_revision": expected,
        "decision": "approved",
        "rights_basis": "s.3(b)",
        "materials": MATERIALS,
        **overrides,
    }
    return browser.post(f"/api/documents/{ACT}/approval", json=body)


def confirm_details(browser, expected=0, **changes):
    """Confirm the suggested details, accounting for every one the test document does not state."""
    current = browser.get(f"/api/documents/{ACT}/details").json()
    given = {"text.form": "certified_copy", **current["record"]["details"], **changes}
    absent = {key: "unknown" for key in current["missing"] if key not in given}
    absent.update({k: v for k, v in current["record"]["absent"].items() if k not in given})
    body = {"expected_revision": expected, "status": "confirmed", "details": given, "absent": absent}
    return browser.post(f"/api/documents/{ACT}/details", json=body)


def test_approval_waits_for_every_page_and_high_flag_and_binds_the_content(world):
    approver = signed_in(world, "approver@example.org")
    refused = approve(approver)
    assert refused.status_code == 409 and refused.json()["readiness"]["unchecked_pages"] == [1, 2]
    assert refused.json()["readiness"]["details"]["confirmed"] is False
    save(approver)  # page 1 checked, but its high flag is not resolved
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    assert approve(approver).json()["readiness"]["open_high_flags"] == [{"page": 1, "id": flag_id()}]
    save(approver, expected=1, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    assert approve(approver).status_code == 409  # the pages are done; the details are not confirmed
    assert confirm_details(approver).json() == {"revision": 1}
    assert approve(approver, materials={**MATERIALS, "dates": False}).status_code == 422
    assert approve(approver, rights_basis=" ").status_code == 422
    assert approve(approver).json() == {"revision": 1}
    exported = approver.get(f"/api/documents/{ACT}/export").json()
    assert exported["approved"] and exported["pages"][0]["units"][0]["text"].endswith(
        "section 199 of this Act."
    )
    assert "[Margin note: Accounts]" in approver.get(f"/api/documents/{ACT}/text").text
    save(
        approver,
        expected=2,
        units=[{"kind": "paragraph", "text": "1. Accounts prepared under section 199 of this Act!"}],
        resolutions={flag_id(): {"decision": "edited"}},
    )
    assert (
        approver.get(f"/api/documents/{ACT}/export").json()["approved"] is False
    )  # an edit after approval voids it
    listed = approver.get("/api/documents").json()["documents"][0]
    assert listed["approval"] == "out_of_date" and listed["status"] == "Approval out of date"
    actions = [e["action"] for e in approver.get(f"/api/audit?act={ACT}").json()]
    assert "act_approved" in actions and actions.count("page_saved") == 4 and "details_saved" in actions


def test_approval_covers_the_details_and_changing_them_voids_it(world):
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    confirm_details(approver)
    assert approve(approver).json() == {"revision": 1}
    detail = approver.get(f"/api/documents/{ACT}").json()
    assert detail["approved"] and detail["approvals"][0]["details_sha256"] == detail["details_sha256"]
    xml = approver.get(f"/api/documents/{ACT}/akn").text
    assert f'detailsSha256="{detail["details_sha256"]}"' in xml and 'standard="document-record-1"' in xml
    assert '<FRBRthis value="/akn/ng/act/' in xml
    assert (
        confirm_details(approver, expected=1, **{"status.note": "Checked against the gazette"}).status_code
        == 200
    )
    assert approver.get(f"/api/documents/{ACT}/export").json()["approved"] is False


def test_details_are_validated_and_confirming_needs_every_detail_accounted_for(world):
    reviewer = signed_in(world, "reviewer@example.org")
    current = reviewer.get(f"/api/documents/{ACT}/details").json()
    assert (
        current["record"]["status"] == "suggested" and current["standard"]["standard"] == "document-record-1"
    )
    given = current["record"]["details"]
    assert given["identity.family"] == "legislation" and given["identity.type"] == "act"
    keys = {f["key"] for f in current["standard"]["fields"]}
    # Nigeria's procedure gives the steps: passed by each House, certified, assented, published, commenced.
    assert {
        "step:passed:senate.date",
        "step:passed:house.date",
        "step:assented.date",
        "step:assented.person",
    } <= keys
    assert current["standard"]["kinds"]["judgment"][0] == "judgment"
    act = {"identity.family": "legislation", "identity.type": "act"}

    def post(**body):
        return reviewer.post(f"/api/documents/{ACT}/details", json={"expected_revision": 0, **body})

    assert post(status="confirmed", details={**act, "identity.title": "x"}).json()["missing"]
    assert post(status="draft", details={**act, "step:assented.date": "2023-02-30"}).status_code == 422
    assert post(status="draft", details={**act, "status.legal": "maybe"}).status_code == 422
    assert post(status="draft", details={**act, "nonsense": "x"}).status_code == 422
    assert (
        post(
            status="draft", details={**act, "identity.family": "legislation", "identity.type": "judgment"}
        ).status_code
        == 422
    )
    gap = post(status="draft", details={**act, "identity.year": "2023"}, absent={"identity.year": "unknown"})
    assert gap.status_code == 422
    bad_link = {**act, "relations": [{"kind": "inspires", "target": "Another Act"}]}
    assert post(status="draft", details=bad_link).status_code == 422
    rows = {**act, "expressions": [{"language": "en", "authority": "sometimes"}]}
    assert post(status="draft", details=rows).status_code == 422
    assert post(status="draft", details={**act, "identity.title": "Act, 2015"}).json() == {"revision": 1}
    assert post(status="draft", details={**act, "identity.title": "Act, 2015"}).status_code == 409  # stale
    assert reviewer.get(f"/api/documents/{ACT}/details").json()["record"]["status"] == "draft"


def test_the_details_record_each_versions_authority_and_the_language_rule_with_evidence(world):
    from schema.jurisdictions import registry
    from workbench.review import Review

    reviewer = signed_in(world, "reviewer@example.org")
    fields = {f["key"]: f for f in reviewer.get(f"/api/documents/{ACT}/details").json()["standard"]["fields"]}
    assert fields["language_rule.rule"]["accountable"] and not fields["language_rule.rule"]["required"]
    words = "This Act is made in English."
    expressions = [
        {"language": "en", "authority": "authoritative", "authority_page": "1", "authority_words": words,
         "this_text": "yes"},
        {"language": "fr", "this_text": "no"},  # nothing states it
    ]  # fmt: skip
    rule = {"language_rule.rule": "Where a translation differs, the English text prevails."}
    refused = confirm_details(reviewer, expressions=expressions, **rule)
    assert refused.status_code == 422 and "where the language rule is stated" in refused.json()["message"]
    rule |= {"language_rule.prevails": "en", "language_rule.evidence.page": "1"}
    assert confirm_details(reviewer, expressions=expressions, **rule).json() == {"revision": 1}
    store, library = world
    review, act = Review(store, library, registry()), library.act(ACT)
    record = review.built(act, review.state(act)["view"])
    english, french = record.expressions
    assert (english.authority, english.authority_evidence.page, english.authority_evidence.quote) == (
        "authoritative",
        1,
        words,
    )
    assert french.authority is None  # published as unknown
    assert record.language_rule.prevails == "en" and record.language_rule.evidence.page == 1


def test_a_judgment_gets_the_case_fields_and_a_record_that_validates(world):
    reviewer = signed_in(world, "reviewer@example.org")
    judgment = {"identity.family": "judgment", "identity.type": "judgment"}
    saved = reviewer.post(
        f"/api/documents/{ACT}/details", json={"expected_revision": 0, "status": "draft", "details": judgment}
    )
    assert saved.json() == {"revision": 1}
    fields = {f["key"] for f in reviewer.get(f"/api/documents/{ACT}/details").json()["standard"]["fields"]}
    assert {"profile.court.name", "profile.parties", "profile.bench", "event:decided.date"} <= fields
    assert "step:passed:senate.date" not in fields  # Nigeria's law-making steps are for Acts


def test_details_are_suggested_from_the_gazette_the_certificate_and_the_assent():
    page = {
        "page": 1,
        "text": "Extraordinary Federal Republic of Nigeria Official Gazette No. 56 Lagos - 27th March, 2023 "
        "Vol. 110 Government Notice No. 24",
        "tables": [[["Act No.", "Short Title", "Page"], ["8", "Copyright Act, 2022 .. ..", "A179-243"]]],
        "furniture": ["Printed and Published by The Federal Government Printer, Lagos, Nigeria"],
        "nodes": [
            {"kind": "heading", "text": "EXPLANATORY MEMORANDUM"},
            {"kind": "paragraph", "text": "This Act."},
        ],
    }
    body = {
        "page": 2,
        "text": "An Act to repeal the Copyright Act, Cap C28, Laws of the Federation of Nigeria, 2004 and enact "
        "the Copyright Act 2022; and for related matters. [17th Day of March, 2023] Commencement "
        "ENACTED by the National Assembly of the Federal Republic of Nigeria — "
        "This Act may be cited as the Copyright Act, 2022.",
        "tables": [],
        "nodes": [],
    }
    end = {
        "page": 3,
        "text": "Date Passed by the Senate Date Passed by the House of Representatives 6th April, 2022. "
        "27th July, 2022. I certify that this Bill ... Acts Authentication Act Cap. A2, 2004. I ASSENT LS "
        "OJO O. A., fnia, fcia Clerk to the National Assembly 13th Day of February, 2023. "
        "MUHAMMADU BUHARI, GCFR President of the Federal Republic of Nigeria 17th Day of March, 2023.",
        "tables": [],
        "nodes": [],
    }
    found = details.suggest({"pages": [page, body, end]}, {"id": "x", "type": "act"}, {"jurisdiction": "ng"})
    value = {key: s.get("value") for key, s in found.items()}
    assert value["gazette_number"] == "56" and value["gazette_date"] == "2023-03-27"
    assert value["gazette_volume"] == "110" and value["gazette_issue"] == "extraordinary"
    assert value["number"] == "8" and value["gazette_pages"] == "A179-243"
    assert value["official_publisher"] == "Federal Government Printer, Lagos"
    assert value["citation"] == "Copyright Act, No. 8, 2022" and value["text_form"] == "gazette_supplement"
    assert value["commencement_date"] == "2023-03-17" and value["summary"] == "This Act."
    assert value["passed_senate"] == "2022-04-06" and value["passed_house"] == "2022-07-27"
    assert value["authenticated_on"] == "2023-02-13"
    assert value["authenticated_by"] == "Ojo O. A., fnia, fcia, Clerk to the National Assembly"
    assert value["assent_date"] == "2023-03-17"
    assert value["assented_by"] == "Muhammadu Buhari, GCFR, President of the Federal Republic of Nigeria"
    assert value["relations"] == [
        {"kind": "repeals", "target": "Copyright Act, Cap C28, Laws of the Federation of Nigeria, 2004"}
    ]
    assert found["gazette_number"]["page"] == 1 and found["assent_date"]["page"] == 3
    assert details.initial(found)[0]["gazette_issue"] == "extraordinary"  # printed: in the first draft


def test_an_issue_inferred_to_be_ordinary_is_suggested_not_filled_in():
    """An ordinary gazette does not print "Ordinary": inferred from the absence of "Extraordinary", it is
    suggested beside its field and left out of the first draft, which keeps what the front page prints."""
    front = {
        "page": 1,
        "text": "Federal Republic of Nigeria Official Gazette No. 12 Lagos - 4th May, 2023 Vol. 110",
        "tables": [],
        "furniture": [],
        "nodes": [],
    }
    found = details.suggest({"pages": [front]}, {"id": "x", "type": "act"}, {"jurisdiction": "ng"})
    assert found["gazette_issue"] == {
        "value": "ordinary",
        "page": 1,
        "evidence": "The gazette front page does not print the word Extraordinary",
        "inferred": True,
    }
    values, _ = record_view.first_draft(*record_view.from_legacy(*details.initial(found)))
    assert "publication.issue" not in values and values["publication.number"] == "12"
    assert record_view.suggestions_view(found)["publication.issue"]["value"] == "ordinary"


def test_a_certified_bill_marks_the_gazette_details_as_not_in_this_copy():
    body = {
        "page": 1,
        "text": "An Act to amend the X Act, No. 1, 2015; and for related matters. "
        "[ ] Commencement ENACTED by the National Assembly of the Federal Republic of Nigeria —",
        "tables": [],
        "nodes": [],
    }
    end = {
        "page": 2,
        "text": "I certify that this Bill ... Acts Authentication Act Cap. A2, 2004. "
        "SANI MAGAJI TAMBAWAL, fcna Clerk to the National Assembly 27th Day of February, 2024",
        "tables": [],
        "nodes": [],
    }
    found = details.suggest(
        {"pages": [body, end]}, {"id": "x", "type": "act", "title": "X (Amendment) Act, 2024"}, {}
    )
    assert found["text_form"]["value"] == "authenticated_bill"
    assert (
        found["gazette_number"]["absent"] == "not_in_source" and found["number"]["absent"] == "not_in_source"
    )
    assert found["commencement_date"]["absent"] == "not_in_source"
    assert found["assent_date"]["absent"] == "not_in_source"
    assert found["authenticated_by"]["value"] == "Sani Magaji Tambawal, fcna, Clerk to the National Assembly"
    assert found["relations"]["value"] == [{"kind": "amends", "target": "X Act, No. 1, 2015"}]


# --- OpenID Connect ----------------------------------------------------------------------------------

ISSUER = "https://accounts.example.org"


def id_token(**claims):
    payload = {
        "iss": ISSUER,
        "aud": "client-1",
        "exp": time.time() + 300,
        "email": "reviewer@example.org",
        "email_verified": True,
        **claims,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{encoded}.signature"


def provider(claims_for_nonce):
    def handle(request: httpx.Request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200, json={"authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": f"{ISSUER}/token"}
            )
        form = parse_qs(request.content.decode())
        assert (
            form["code_verifier"][0] and form["redirect_uri"][0] == "https://review.example.org/auth/callback"
        )
        return httpx.Response(200, json={"id_token": claims_for_nonce()})

    return httpx.Client(transport=httpx.MockTransport(handle))


def oidc_flow(world, **claims):
    store, _ = world
    holder = {}
    config = auth.OIDCConfig(ISSUER, "client-1", "secret", "https://review.example.org")
    oidc = auth.OIDC(config, provider(lambda: id_token(nonce=claims.pop("nonce", holder["nonce"]), **claims)))
    browser = client(world, oidc=oidc, dev=False, host="review.example.org", scheme="https")
    location = browser.get("/auth/login", follow_redirects=False).headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == [
        "https://review.example.org/auth/callback"
    ]
    with store.engine.connect() as db:
        from sqlalchemy import select

        from workbench.store import auth_flow

        holder["nonce"] = db.execute(select(auth_flow.c.nonce)).scalar()
    return browser, query["state"][0]


def test_oidc_sign_in_admits_a_verified_operator_once(world):
    browser, state = oidc_flow(world)
    signed = browser.get(f"/auth/callback?state={state}&code=abc")
    assert signed.status_code == 200 and "Signed in" in signed.text
    cookie = signed.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    again = browser.get(f"/auth/callback?state={state}&code=abc", follow_redirects=False)  # one use only
    assert again.status_code == 303 and again.headers["location"].startswith("/?signin=")


def test_a_sign_in_must_finish_in_the_browser_that_started_it(world):
    browser, state = oidc_flow(world)
    store, library = world
    elsewhere = TestClient(browser.app, base_url="https://review.example.org")  # no flow cookie
    response = elsewhere.get(f"/auth/callback?state={state}&code=abc", follow_redirects=False)
    assert response.headers["location"] == "/?signin=other_browser"
    assert "oa_review_session" not in response.headers.get("set-cookie", "")
    browser.cookies.set("oa_review_flow", "another-flow", domain="review.example.org", path="/auth")
    refused = browser.get(f"/auth/callback?state={state}&code=abc", follow_redirects=False)
    assert refused.headers["location"] == "/?signin=other_browser"
    assert "oa_review_session" not in browser.cookies


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"aud": "someone-else"}, "failed"),
        ({"iss": "https://evil.example.org"}, "failed"),
        ({"nonce": "replayed"}, "expired"),
        ({"exp": time.time() - 10}, "expired"),
        ({"email_verified": False}, "unverified"),
        ({"email": "stranger@example.org"}, "not_operator"),
    ],
)
def test_oidc_sign_in_rejects_bad_claims_and_strangers_back_on_the_sign_in_page(world, claims, reason):
    # Refused sign-ins go back to the sign-in page with a reason from a fixed list, never free text.
    browser, state = oidc_flow(world, **claims)
    response = browser.get(f"/auth/callback?state={state}&code=abc", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/?signin={reason}"
    assert "oa_review_session" not in response.headers.get("set-cookie", "")


# --- document surface support and Akoma Ntoso --------------------------------------------------------


def test_unit_levels_are_kept_and_bounded(world):
    browser = signed_in(world, "reviewer@example.org")
    assert save(browser, units=[{"kind": "paragraph", "text": "(a) any law", "level": 2}]).status_code == 200
    assert browser.get(f"/api/documents/{ACT}/pages/1").json()["latest"]["units"][0]["level"] == 2
    for bad in (4, -1, "2", True):
        assert (
            save(browser, expected=1, units=[{"kind": "paragraph", "text": "x", "level": bad}]).status_code
            == 422
        )


def test_page_regions_come_from_the_positional_readers_output(world):
    store, library = world
    folder = library.staging / "readings" / ACT
    folder.mkdir(parents=True)
    analyze = {
        "pages": [{"pageNumber": 1, "width": 8.5, "height": 11}],
        "paragraphs": [
            {
                "content": "1. Accounts prepared",
                "boundingRegions": [{"pageNumber": 1, "polygon": [1, 2, 7.5, 2, 7.5, 3.1, 1, 3.1]}],
            }
        ],
    }
    (folder / "azure.json").write_text(json.dumps({"raw": {"analyze": analyze}}))
    browser = signed_in(world, "reviewer@example.org")
    region = browser.get(f"/api/documents/{ACT}/pages/1").json()["regions"][0]
    assert region["text"] == "1. Accounts prepared"
    assert [round(v, 3) for v in region["box"]] == [
        round(1 / 8.5, 3),
        round(2 / 11, 3),
        round(7.5 / 8.5, 3),
        round(3.1 / 11, 3),
    ]


def test_akoma_ntoso_export_is_well_formed_and_structured(world):
    import xml.etree.ElementTree as ET

    from workbench import akn

    browser = signed_in(world, "reviewer@example.org")
    units = [
        {"kind": "heading", "text": "TEST ACT"},
        {"kind": "paragraph", "text": "ENACTED by the National Assembly —"},
        {"kind": "heading", "text": "PART I — PRELIMINARY"},
        {"kind": "paragraph", "text": "80. (1) The Commission —", "notes": ["Promotion of renewables"]},
        {"kind": "list_item", "marker": "(a)", "text": "shall promote; and"},
        {"kind": "paragraph", "text": "(2) In granting licences."},
    ]
    save(browser, units=units)
    response = browser.get(f"/api/documents/{ACT}/akn")
    assert response.headers["content-type"].startswith("application/akn+xml")
    root = ET.fromstring(response.content)
    ns = {"a": akn.NS}
    assert root.find("a:act/a:preamble/a:formula", ns).get("name") == "enactingText"
    section = root.find("a:act/a:body/a:part/a:section", ns)
    assert section.get("eId") == "sec_80" and section.find("a:heading", ns).text == "Promotion of renewables"
    subsections = section.findall("a:subsection", ns)
    assert [s.get("eId") for s in subsections] == ["sec_80__subsec_1", "sec_80__subsec_2"]
    assert subsections[0].find("a:paragraph", ns).get("eId") == "sec_80__subsec_1__para_a"
    assert root.find("a:act/a:meta/a:identification/a:FRBRWork/a:FRBRcountry", ns).get("value") == "ng"


# --- Act-wide tools ------------------------------------------------------------------------------


def test_search_and_replace_across_pages_writes_attributed_revisions(world):
    browser = signed_in(world, "reviewer@example.org")
    found = browser.get(f"/api/documents/{ACT}/search", params={"q": "section"}).json()
    assert [(h["page"], h["match"]) for h in found["hits"]] == [(1, "section")]
    assert (
        browser.get(f"/api/documents/{ACT}/search", params={"q": "Sect", "case": True}).json()["hits"] == []
    )
    assert (
        browser.get(f"/api/documents/{ACT}/search", params={"q": "sect", "word": True}).json()["hits"] == []
    )
    assert browser.get(f"/api/documents/{ACT}/search", params={"q": ""}).status_code == 422
    result = browser.post(
        f"/api/documents/{ACT}/replace",
        json={
            "find": "198",
            "replace": "199",
            "word": True,
            "pages": {str(p): r for p, r in found["revisions"].items()},
        },
    ).json()
    assert result == {"changed": [{"page": 1, "revision": 1, "replacements": 1}], "conflicts": []}
    page = browser.get(f"/api/documents/{ACT}/pages/1").json()["latest"]
    assert page["units"][0]["text"].endswith("section 199 of this Act.") and page["status"] == "edited"
    assert (
        page["author"] == "reviewer@example.org"
        and "Replaced '198' with '199'"
        in browser.get(f"/api/documents/{ACT}/pages/1").json()["history"][0]["comment"]
    )
    stale = browser.post(
        f"/api/documents/{ACT}/replace", json={"find": "Act", "replace": "ACT", "pages": {"1": 0}}
    ).json()
    assert stale["conflicts"] == [1]  # the preview was older than the page


def test_replacing_across_the_act_keeps_formatting_on_the_same_words():
    units = [
        {
            "kind": "paragraph",
            "text": "The Tax is due; the tax rate is 3%.",
            "marks": [
                {"type": "bold", "start": 4, "end": 7},  # the match itself
                {"type": "italic", "start": 16, "end": 28},  # "the tax rate", a match inside
                {"type": "superscript", "start": 32, "end": 33},  # "3", after both matches
            ],
        },
        {"kind": "paragraph", "text": "Untouched", "marks": [{"type": "bold", "start": 0, "end": 9}]},
    ]
    replaced, count = content.replace_in(units, "tax", "levy", False, True)
    assert count == 2
    assert replaced[0]["text"] == "The levy is due; the levy rate is 3%."
    marked = {m["type"]: replaced[0]["text"][m["start"] : m["end"]] for m in replaced[0]["marks"]}
    assert marked == {"bold": "levy", "italic": "the levy rate", "superscript": "3"}
    assert replaced[1] == units[1] and units[0]["text"].startswith("The Tax")  # a copy; the rest as it was
    removed, _ = content.replace_in(units, "Tax", "", True, True)
    assert removed[0]["text"] == "The  is due; the tax rate is 3%." and all(
        m["type"] != "bold" for m in removed[0]["marks"]
    )
    assert content.validate_units(removed) == removed


def test_the_stream_returns_final_content_for_continuous_reading(world):
    browser = signed_in(world, "reviewer@example.org")
    stream = browser.get(f"/api/documents/{ACT}/stream", params={"start": 1, "count": 5}).json()
    assert stream["total"] == 2 and [p["page"] for p in stream["pages"]] == [1, 2]
    assert stream["pages"][0]["open_high"] == 1 and stream["pages"][0]["units"][0]["notes"] == ["Accounts"]


def test_documents_other_than_legislation_export_as_akn_documents():
    import xml.etree.ElementTree as ET

    from workbench import akn

    judgment = {
        "id": "ng-judgment-2024-1",
        "title": "A v B",
        "year": 2024,
        "type": "judgment",
        "jurisdiction": "ng",
    }
    pages = [
        {
            "page": 1,
            "units": [
                {"kind": "heading", "text": "JUDGMENT"},
                {"kind": "paragraph", "text": "1. The appeal is allowed."},
            ],
        }
    ]
    root = ET.fromstring(akn.build(judgment, pages, "0" * 64))
    ns = {"a": akn.NS}
    doc = root.find("a:doc", ns)
    assert doc.get("name") == "judgment"
    assert (
        doc.find("a:meta/a:identification/a:FRBRWork/a:FRBRuri", ns).get("value")
        == "/akn/ng/doc/2024/judgment-2024-1"
    )
    assert [p.text for p in doc.findall("a:mainBody/a:p", ns)] == ["1. The appeal is allowed."]


def test_overview_summarises_documents_and_where_to_continue(world):
    browser = signed_in(world, "reviewer@example.org")
    first = browser.get("/api/overview").json()
    assert first["totals"] == {
        "documents": 1,
        "pages": 2,
        "checked": 0,
        "open_high": 1,
        "approved": 0,
        "ready": 0,
    }
    assert first["types"] == ["act"] and first["resume"] is None
    listed = browser.get("/api/documents").json()
    assert listed["total"] == 1 and listed["documents"][0]["status"] == "Not started"
    save(browser)  # page 1 checked
    after = browser.get("/api/overview").json()
    assert after["resume"]["page"] == 2 and after["resume"]["id"] == ACT
    assert after["activity"][0]["action"] == "page_saved"
    row = browser.get("/api/documents").json()["documents"][0]
    assert row["checked"] == 1 and row["status"] == "In review"


def test_unit_alignment_is_kept_and_validated(world):
    browser = signed_in(world, "reviewer@example.org")
    heading = [{"kind": "heading", "text": "PART I — PRELIMINARY PROVISIONS", "align": "left"}]
    assert save(browser, units=heading).status_code == 200
    assert browser.get(f"/api/documents/{ACT}/pages/1").json()["latest"]["units"][0]["align"] == "left"
    justified = [
        {"kind": "paragraph", "marker": "3.", "text": "(1) The Federal Government", "align": "justify"}
    ]
    assert save(browser, expected=1, units=justified).status_code == 200
    assert browser.get(f"/api/documents/{ACT}/pages/1").json()["latest"]["units"][0]["align"] == "justify"
    assert (
        save(browser, expected=2, units=[{"kind": "heading", "text": "x", "align": "middle"}]).status_code
        == 422
    )


def test_character_formatting_is_validated_and_exported_as_akn_inline(world):
    import xml.etree.ElementTree as ET

    from workbench import akn

    browser = signed_in(world, "reviewer@example.org")
    unit = {
        "kind": "paragraph",
        "marker": "(a)",
        "text": "the 1st Schedule applies",
        "marks": [{"type": "superscript", "start": 5, "end": 7}, {"type": "italic", "start": 8, "end": 16}],
    }
    assert save(browser, units=[unit]).status_code == 200
    stored = browser.get(f"/api/documents/{ACT}/pages/1").json()["latest"]["units"][0]
    assert stored["marks"] == unit["marks"]
    assert (
        save(
            browser, expected=1, units=[{**unit, "marks": [{"type": "bold", "start": 0, "end": 99}]}]
        ).status_code
        == 422
    )
    assert (
        save(
            browser, expected=1, units=[{**unit, "marks": [{"type": "font-size", "start": 0, "end": 2}]}]
        ).status_code
        == 422
    )
    p = akn.unit_paragraph(unit)
    ns = {"a": akn.NS}
    assert "".join(p.itertext()) == "(a) the 1st Schedule applies"
    assert p.find("a:sup", ns).text == "st" and p.find("a:i", ns).text == "Schedule"
    ET.tostring(p)  # well formed


def test_the_public_preview_shows_the_latest_saved_pages_as_readers_would(world):
    browser = signed_in(world, "reviewer@example.org")
    unit = {
        "kind": "paragraph",
        "marker": "1.",
        "marker_source": ["azure"],
        "text": "Accounts prepared under section 199 of this Act.",
        "marks": [{"type": "bold", "start": 0, "end": 8}],
        "notes": ["Accounts"],
        "align": "justify",
    }
    assert save(browser, units=[unit]).status_code == 200
    preview = browser.get(f"/api/documents/{ACT}/public-preview").json()
    source = preview["source"]
    assert source["id"] == ACT and source["document_type"] == "act" and source["jurisdiction_id"] == "ng"
    # Rights and currentness are not asserted by the preview: the public defaults show.
    assert source["rights_status"] == "RIGHTS_UNDER_REVIEW" and source["legal_status"] == "unknown"
    first, second = preview["pages"]
    assert first["checked"] and not second["checked"]
    assert first["blocks"] == [{key: value for key, value in unit.items() if key != "marker_source"}]
    assert second["blocks"] == [{"kind": "paragraph", "text": "2. Citation."}]
    assert {k: preview["status"][k] for k in ("approved", "pages", "checked_pages")} == {
        "approved": False,
        "pages": 2,
        "checked_pages": 1,
    }
    assert (
        preview["details"]["status"] == "suggested"
        and preview["details"]["details"]["identity.family"] == "legislation"
    )
    assert preview["review"]["reviewers"] == ["Rev"] and preview["review"]["approval"] is None
    assert preview["review"]["readers"]["primary"] == "Google Document AI Layout Parser"
    assert [d["format"] for d in preview["downloads"]] == ["TXT", "XML", "PDF"]
    page = browser.get(f"/preview/{ACT}")
    assert page.status_code == 200 and "/static/public-preview/preview.js" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]


def rwanda_collection(folder, monkeypatch):
    """A second collection beside the first: one Rwandan law in two of its language versions, each
    a reading taken from the gazette's text layer, with its boxes and a flag for comparison."""
    from ingestion import rw_gazette as RG

    folder.mkdir()
    lines = {
        "en": [{"text": "Article One: Purpose", "x0": 300, "y0": 70, "x1": 420, "y1": 80, "page": 3}],
        "fr": [{"text": "Article premier : Objet", "x0": 530, "y0": 70, "x1": 660, "y1": 80, "page": 3}],
    }
    documents = []
    for language, printed in lines.items():
        nodes = RG.structure([{**line, "text": "We, EXAMPLE Name,"} for line in printed] + printed, language)
        nodes[-1].setdefault("flags", []).append({"severity": "low", "kind": "heading_form", "primary": "x"})
        reading = RG.reading(
            nodes, {3: ["Official Gazette n° 11 of 16/03/2026"]}, "0" * 64, {}, {3: (842.0, 595.0)}
        )
        doc_id = f"rw-law-009-2026-{language}"
        (folder / f"{doc_id}.reading.json").write_text(json.dumps(reading))
        documents.append(
            {
                "id": doc_id,
                "type": "law",
                "title": "Law n° 009/2026 of 12/03/2026 governing examples",
                "year": 2026,
                "jurisdiction": "rw",
                "language": language,
                "work": "rw-law-009-2026",
                "suggested": {
                    "identity.family": "legislation",
                    "identity.type": "law",
                    "identity.number.value": "009/2026",
                    "step:promulgated.person": "EXAMPLE Name, President of the Republic",
                    "expressions": [{"language": "rw"}, {"language": "en"}, {"language": "fr"}],
                },
            }
        )
    (folder / "manifest.json").write_text(
        json.dumps({"collection": "rw-test", "jurisdiction": "rw", "documents": documents})
    )
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(folder))


def test_a_second_collection_reviews_each_language_version_and_shares_the_laws_details(tmp_path, monkeypatch):
    rwanda_collection(tmp_path / "rw", monkeypatch)
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    world = (store, content.Library(tmp_path))
    reviewer = signed_in(world, "reviewer@example.org")
    ids = [d["id"] for d in reviewer.get("/api/documents?limit=200").json()["documents"]]
    assert ACT in ids and {"rw-law-009-2026-en", "rw-law-009-2026-fr"} <= set(ids)
    page = reviewer.get("/api/documents/rw-law-009-2026-fr/pages/3").json()
    assert page["regions"][0]["box"][0] > 0.6  # the French column, on the right of the page
    assert "Article premier : Objet" in [line["text"] for line in page["lines"]]
    assert page["flags"][0]["compare"] is True
    details = reviewer.get("/api/documents/rw-law-009-2026-en/details").json()
    fields = {f["key"] for f in details["standard"]["fields"]}
    assert "step:sealed.person" in fields  # Rwanda's procedure, not Nigeria's
    values = details["record"]["details"]
    assert values["step:promulgated.person"] == "EXAMPLE Name, President of the Republic"
    assert [row["this_text"] for row in values["expressions"]] == ["no", "yes", "no"]
    saved = reviewer.post(
        "/api/documents/rw-law-009-2026-en/details",
        json={"expected_revision": 0, "status": "draft", "details": {**values, "identity.year": "2026"}},
    )
    assert saved.json() == {"revision": 1}
    french = reviewer.get("/api/documents/rw-law-009-2026-fr/details").json()
    assert french["history"][0]["revision"] == 1  # one record for the law, whichever version is open
    assert [row["this_text"] for row in french["record"]["details"]["expressions"]] == ["no", "no", "yes"]


def test_a_rwandan_laws_authority_is_suggested_with_its_words_and_stays_blank_until_the_reviewer_uses_it(
    tmp_path, monkeypatch
):
    """Owner decision 2026-10-21: machines may suggest legal facts, never pre-fill them. The gazette reader
    suggests what each language version is worth as law, with the sentence that says so and its page; the
    field stays empty until the reviewer presses "Use suggestion", and a record confirmed without it
    publishes the authority as unknown."""
    from test_rw_gazette import read_law

    from operations import publish
    from schema.jurisdictions import registry
    from workbench.review import Review

    values, legal, inferred, _ = read_law()
    folder = tmp_path / "rw"
    rwanda_collection(folder, monkeypatch)
    manifest = json.loads((folder / "manifest.json").read_text())
    for document in manifest["documents"]:
        document |= {"suggested": values, "suggested_legal": legal, "suggested_inferred": inferred}
    (folder / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    library = content.Library(tmp_path)
    reviewer = signed_in((store, library), "reviewer@example.org")
    shown = reviewer.get("/api/documents/rw-law-009-2026-en/details").json()
    draft = shown["record"]["details"]
    # The details as printed are filled in; the legal facts are not.
    assert draft["identity.number.printed"] == "Law n° 009/2026" and draft["publication.date"] == "2026-03-16"
    assert [{k for k in row if k.startswith("authority")} for row in draft["expressions"]] == [set()] * 3
    assert "step:commenced.date" not in draft and "profile.commencement" not in draft
    assert "step:commenced.date" in shown["missing"]  # to give or account for, not taken from the reader
    # Beside the empty fields, what the editor shows: each suggestion with its words and page.
    adopted = "This Law was drafted in English, considered and adopted in Ikinyarwanda."
    authorities = shown["suggestions"]["expressions"]["authority"]
    assert authorities["en"] == {"value": "official_translation", "page": 2, "evidence": adopted}
    assert authorities["rw"]["value"] == "authoritative"
    # What the reader inferred rather than read where it is printed for its step is suggested the same way:
    # the one date above the signatures for the countersignature and the seal, and the Senate's absence
    # from a preamble naming only the Chamber of Deputies.
    assert draft["step:promulgated.date"] == "2026-03-12"  # printed for the President's signature
    kigali = {"value": "2026-03-12", "page": 2, "evidence": "Kigali, 12/03/2026"}
    for step in ("step:countersigned.date", "step:sealed.date"):
        assert step not in draft and step in shown["missing"] and shown["suggestions"][step] == kigali
    assert "step:adopted:senate.date" not in shown["record"]["absent"]
    assert shown["suggestions"]["step:adopted:senate.date"] == {
        "absent": "not_applicable",
        "page": 2,
        "evidence": "The Chamber of Deputies, in its sitting of 20 February 2026;",
    }
    assert shown["suggestions"]["step:commenced.date"] == {
        "value": "2026-03-16",
        "page": 2,
        "evidence": "This Law comes into force on the date of its publication.",
    }
    columns = next(f for f in shown["standard"]["fields"] if f["key"] == "expressions")["columns"]
    assert "authority" in {c["key"] for c in columns}  # the column the suggestion belongs to

    act = library.act("rw-law-009-2026-en")
    review = Review(store, library, registry())

    def confirm(details: dict, expected: int):
        current = reviewer.get("/api/documents/rw-law-009-2026-en/details").json()
        given = {**details, "step:commenced.date": "2026-03-16"}
        absent = {**current["record"]["absent"], **dict.fromkeys(current["missing"], "unknown")}
        absent = {key: reason for key, reason in absent.items() if key not in given}
        body = {"expected_revision": expected, "status": "confirmed", "details": given, "absent": absent}
        assert reviewer.post("/api/documents/rw-law-009-2026-en/details", json=body).json() == {
            "revision": expected + 1
        }
        return review.built(act, review.state(act)["view"])

    # Confirmed without pressing "Use suggestion": every authority unknown, none published.
    record = confirm(draft, 0)
    assert [e.authority for e in record.expressions] == [None, None, None]
    assert publish.editions(record) == []
    # Pressing it (what the button does to the row): the authority, with its page and words as evidence.
    evidence = {"authority_page": "2", "authority_words": adopted}
    used = [
        {**row, "authority": authorities[row["language"]]["value"], **evidence}
        for row in draft["expressions"]
    ]
    record = confirm({**draft, "expressions": used}, 1)
    english = next(e for e in record.expressions if e.language == "en")
    assert (english.authority, english.authority_evidence.page, english.authority_evidence.quote) == (
        "official_translation",
        2,
        adopted,
    )
    assert {e.language: e.authority for e in publish.editions(record)} == {
        "rw": "authoritative",
        "en": "official_translation",
        "fr": "official_translation",
    }


def test_a_nigerian_first_draft_leaves_the_legal_facts_to_the_reviewer(world):
    """The Nigerian reader's suggestions fill in the first draft, but not its legal facts: what the text is
    worth as law, when it commences, the laws it affects. Those stay beside their fields."""
    reviewer = signed_in(world, "reviewer@example.org")
    shown = reviewer.get(f"/api/documents/{ACT}/details").json()
    draft, absent = shown["record"]["details"], shown["record"]["absent"]
    assert draft["expressions"] == [{"language": "en", "direction": "ltr", "this_text": "yes"}]  # unknown
    assert draft["status.legal"] == "unknown"  # not yet checked, which states nothing
    # The reader found no commencement date; it suggests "not stated in this copy" but does not say so for you.
    assert "step:commenced.date" not in draft and "step:commenced.date" not in absent
    assert shown["suggestions"]["step:commenced.date"]["absent"] == "not_in_source"
    assert "step:commenced.date" in shown["missing"]


def test_the_public_preview_of_a_language_version_lists_the_others(tmp_path, monkeypatch):
    rwanda_collection(tmp_path / "rw", monkeypatch)
    manifest = json.loads((tmp_path / "rw" / "manifest.json").read_text())
    for document in manifest["documents"]:
        document["versions"] = {
            "en": "rw-law-009-2026-en",
            "fr": "rw-law-009-2026-fr",
            "rw": "rw-law-009-2026-rw",
        }
    (tmp_path / "rw" / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    reviewer = signed_in((store, content.Library(tmp_path)), "reviewer@example.org")
    preview = reviewer.get("/api/documents/rw-law-009-2026-fr/public-preview").json()
    assert preview["source"]["language"] == "fr"
    # The Kinyarwanda version is not in this collection: only versions that exist are offered.
    assert [(v["language"], v["id"]) for v in preview["versions"]] == [
        ("en", "rw-law-009-2026-en"),
        ("fr", "rw-law-009-2026-fr"),
    ]
    languages = reviewer.get("/api/documents/rw-law-009-2026-fr/languages").json()
    assert languages["languages"] == ["en", "fr"] and languages["differences"] == []
    row = languages["rows"][0]
    assert row["article"] == "1" and row["versions"]["fr"]["marker"] == "Article premier :"


def test_review_time_is_measured_per_page_leaving_breaks_out():
    from datetime import UTC, datetime, timedelta

    from workbench.timing import timings

    start = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    saves = [(0, 1), (4, 2), (10, 3), (70, 4), (73, 5)]  # minutes after nine, page; a break before page 4
    rows = [
        {
            "act_id": "rw-law-009-2026-en",
            "page": page,
            "status": "checked",
            "author": "a@example.org",
            "created_at": start + timedelta(minutes=minutes),
        }
        for minutes, page in saves
    ]
    measured = timings(rows, gap_minutes=20)
    assert (
        measured["pages_timed"] == 3
    )  # pages 2, 3 and 5; the first page and the one after the break are not timed
    assert measured["minutes_per_page"]["median"] == 4.0
    assert measured["documents"]["rw-law-009-2026-en"] == {"pages_timed": 3, "minutes": 13.0}


def test_a_table_with_merged_cells_lines_up_and_keeps_its_merges():
    cells = [
        [
            {"text": "Annual income", "row_span": 1, "col_span": 2},
            {"text": "Tax rate", "row_span": 2, "col_span": 1},
        ],
        [{"text": "From", "row_span": 1, "col_span": 1}, {"text": "To", "row_span": 1, "col_span": 1}],
        [{"text": "0"}, {"text": "360,000"}, {"text": "0%"}],
    ]
    rows, spans = content.table_grid(cells)
    assert rows == [["Annual income", "", "Tax rate"], ["From", "To", ""], ["0", "360,000", "0%"]]
    assert spans == [[0, 0, 1, 2], [0, 2, 2, 1]]
    unit = {"kind": "table", "header_rows": 2, "rows": rows, "spans": spans}
    assert content.validate_units([unit])[0]["spans"] == spans
    with pytest.raises(ValueError):
        content.validate_units([{**unit, "spans": [[1, 2, 3, 1]]}])  # runs past the last row


def test_a_merged_cell_is_exported_once_with_its_rowspan_and_colspan():
    from workbench import akn

    unit = {
        "kind": "table",
        "header_rows": 2,
        "rows": [
            ["Annual Taxable Income", "", "Tax Rate"],
            ["From", "To", ""],
            ["0", "360,000", "0%"],
            ["360,001", "1,200,000", "20%"],
        ],
        "spans": [[0, 0, 1, 2], [0, 2, 2, 1]],
        "cell_marks": {"0,2": [{"type": "bold", "start": 0, "end": 8}]},
    }
    ns = {"a": akn.NS}
    rows = akn.table(unit).findall("a:tr", ns)
    cells = [row.findall("*") for row in rows]
    # The covered positions (0,1) and (1,2) are not written; each merged cell is written once.
    assert [[("".join(c.itertext()), c.get("rowspan"), c.get("colspan")) for c in row] for row in cells] == [
        [("Annual Taxable Income", None, "2"), ("Tax Rate", "2", None)],
        [("From", None, None), ("To", None, None)],
        [("0", None, None), ("360,000", None, None), ("0%", None, None)],
        [("360,001", None, None), ("1,200,000", None, None), ("20%", None, None)],
    ]
    assert [c.tag for row in cells for c in row] == [akn.q("th")] * 4 + [akn.q("td")] * 6
    assert cells[0][1].find("a:p/a:b", ns).text == "Tax Rate"  # a merged cell keeps its formatting


def test_a_heading_keeps_the_rank_its_reading_gives_it():
    page = {
        "page": 3,
        "nodes": [
            node("LAW N° 020/2023 ON TAX PROCEDURES", kind="heading", level="heading-1"),
            node("CHAPTER ONE: GENERAL PROVISIONS", kind="heading", level="heading-2"),
            node("Purpose of this Law", kind="heading", level="heading-3"),
            node("Definitions", kind="heading", level="subheading"),
            node("PART I", kind="heading"),  # a reading that does not rank its headings
            node("Words.", level="heading-3"),  # a rank on a paragraph is ignored
        ],
    }
    units = content.units_from(page)
    assert [u.get("rank") for u in units] == [1, 2, 3, None, None, None]
    assert content.validate_units(units) == units
    assert content.public_pages([{"page": 3, "units": units}], {})[0]["blocks"][1] == {
        "kind": "heading",
        "rank": 2,
        "text": "CHAPTER ONE: GENERAL PROVISIONS",
    }


def test_only_a_heading_has_a_rank_and_only_1_to_6():
    heading = {"kind": "heading", "text": "Purpose of this Law"}
    assert content.validate_units([{**heading, "rank": 6}]) == [{**heading, "rank": 6}]
    assert content.validate_units([{**heading, "rank": None}]) == [heading]  # unranked, as saved before
    for rank in (0, 7, True, 2.0, "2"):
        with pytest.raises(ValueError):
            content.validate_units([{**heading, "rank": rank}])
    for unit in (
        {"kind": "paragraph", "text": "Words"},
        {"kind": "list_item", "text": "Words"},
        {"kind": "table", "header_rows": 0, "rows": [["a"]]},
    ):
        with pytest.raises(ValueError):
            content.validate_units([{**unit, "rank": 1}])


def test_formatting_in_table_cells_and_captions_is_kept_moved_and_exported(world):
    import xml.etree.ElementTree as ET

    from workbench import akn, serialize

    browser = signed_in(world, "reviewer@example.org")
    table = {
        "kind": "table",
        "caption": "Table 1: Plot sizes",
        "caption_marks": [{"type": "italic", "start": 0, "end": 7}],
        "header_rows": 1,
        "rows": [["Zone", "Area (m2)"], ["R1", "450 1"], ["Total", ""]],
        "spans": [[2, 0, 1, 2]],
        "cell_marks": {
            "0,1": [{"type": "bold", "start": 0, "end": 4}, {"type": "superscript", "start": 7, "end": 8}],
            "1,1": [{"type": "superscript", "start": 4, "end": 5}],
        },
    }
    assert save(browser, units=[table]).status_code == 200
    assert browser.get(f"/api/documents/{ACT}/pages/1").json()["latest"]["units"] == [table]
    block = browser.get(f"/api/documents/{ACT}/public-preview").json()["pages"][0]["blocks"][0]
    assert block["cell_marks"] == table["cell_marks"] and block["caption_marks"] == table["caption_marks"]

    bold = [{"type": "bold", "start": 0, "end": 1}]
    for wrong in (
        {"cell_marks": {"0,2": bold}},  # no such cell
        {"cell_marks": {"00,1": bold}},  # not how a cell is named
        {"cell_marks": {"2,1": bold}},  # covered by the merged "Total"
        {"cell_marks": {"1,0": [{"type": "bold", "start": 0, "end": 3}]}},  # past the words
        {"cell_marks": {"1,0": [{"type": "font-size", "start": 0, "end": 1}]}},
        {"cell_marks": [["1,0", bold]]},
        {"caption_marks": [{"type": "bold", "start": 0, "end": 99}]},
    ):
        with pytest.raises(ValueError):
            content.validate_units([{**table, **wrong}])
    emptied = content.validate_units([{**table, "cell_marks": {"0,0": []}, "caption_marks": []}])[0]
    assert "cell_marks" not in emptied and "caption_marks" not in emptied
    # Formatting is not in the published text (TXP-02 rule 5).
    plain = {k: v for k, v in table.items() if k not in ("cell_marks", "caption_marks")}
    assert (
        serialize.serialize([{"page": 1, "units": [table]}]).text
        == serialize.serialize([{"page": 1, "units": [plain]}]).text
    )

    (moved,), count = content.replace_in([table], "Area", "Floor area", False, True)
    assert count == 1 and moved["rows"][0][1] == "Floor area (m2)"
    cell = moved["rows"][0][1]
    assert [(m["type"], cell[m["start"] : m["end"]]) for m in moved["cell_marks"]["0,1"]] == [
        ("bold", "Floor area"),
        ("superscript", "2"),
    ]
    (moved,), count = content.replace_in([table], "1", "", False, True)  # the footnote marker goes
    assert count == 2 and moved["caption"] == "Table : Plot sizes" and moved["rows"][1] == ["R1", "450 "]
    assert "1,1" not in moved["cell_marks"] and moved["caption_marks"] == [
        {"type": "italic", "start": 0, "end": 6}
    ]
    assert content.validate_units([moved]) == [moved]

    element = akn.table(table)
    ns = {"a": akn.NS}
    caption = element.find("a:caption", ns)
    assert "".join(caption.itertext()) == "Table 1: Plot sizes" and caption.find("a:i", ns).text == "Table 1"
    header = element.findall("a:tr", ns)[0].findall("a:th", ns)[1].find("a:p", ns)
    assert "".join(header.itertext()) == "Area (m2)"
    assert header.find("a:b", ns).text == "Area" and header.find("a:sup", ns).text == "2"
    assert element.findall("a:tr", ns)[1].findall("a:td", ns)[1].find("a:p/a:sup", ns).text == "1"
    ET.tostring(element)  # well formed


def test_a_collection_in_the_corpus_bucket_is_read_from_it_and_cached(tmp_path, monkeypatch):
    from ingestion import rw_gazette as RG

    lines = [
        {"text": "We, EXAMPLE Name,", "x0": 300, "y0": 60, "x1": 420, "y1": 70, "page": 3},
        {"text": "Article One: Purpose", "x0": 300, "y0": 90, "x1": 420, "y1": 100, "page": 3},
    ]
    reading_json = RG.reading(RG.structure(lines, "en"), {}, "0" * 64, {}, {3: (842.0, 595.0)})
    document = {
        "id": "rw-law-009-2026-en",
        "type": "law",
        "title": "Law n° 009/2026",
        "jurisdiction": "rw",
        "language": "en",
        "work": "rw-law-009-2026",
        "source_object": "sources/rw/minijust-gazette/abc.pdf",
        "objects": "files/abc/",  # the source file's own folder
    }
    objects = {
        "collections/rw/manifest.json": json.dumps({"collection": "rw", "documents": [document]}).encode(),
        "collections/rw/files/abc/rw-law-009-2026-en.reading.json": json.dumps(reading_json).encode(),
        "collections/rw/files/abc/rw-law-009-2026-en.provenance.json": b'{"url": "https://www.minijust.gov.rw/x.pdf"}',
        # What only the document's own page needs, beside it rather than in the manifest.
        "collections/rw/files/abc/rw-law-009-2026-en.document.json": json.dumps(
            {"suggested": {"identity.number": "009/2026"}, "issue": {"name": "OG n° 1"}}
        ).encode(),
        "sources/rw/minijust-gazette/abc.pdf": b"%PDF-1.4 the issue",
    }
    asked = []

    class Bucket:
        def get(self, name):
            asked.append(name)
            return objects[name]

        def exists(self, name):
            return name in objects

    monkeypatch.setattr(content, "_STORES", {"test-bucket": Bucket()})
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", "gs://test-bucket/collections/rw/")
    monkeypatch.setenv("ALVARY_REVIEW_CACHE", str(tmp_path / "cache"))
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    library = content.Library(tmp_path)
    assert "rw-law-009-2026-en" in [d["id"] for d in library.documents()]
    assert library.reading("rw-law-009-2026-en")["pages"][0]["page"] == 3
    assert library.original("rw-law-009-2026-en").read_bytes() == b"%PDF-1.4 the issue"
    assert library.provenance("rw-law-009-2026-en")["url"].endswith("x.pdf")
    library.reading("rw-law-009-2026-en")
    assert (
        asked.count("collections/rw/files/abc/rw-law-009-2026-en.reading.json") == 1
    )  # cached after the first time
    # The list carries no details; the document opened carries them, fetched once.
    assert "suggested" not in library.documents()[-1]
    assert library.act("rw-law-009-2026-en")["suggested"] == {"identity.number": "009/2026"}
    library.act("rw-law-009-2026-en")
    assert asked.count("collections/rw/files/abc/rw-law-009-2026-en.document.json") == 1


def test_a_collection_in_an_azure_blob_container_is_read_from_it_and_cached(tmp_path, monkeypatch):
    from blob_support import BlobService

    from ingestion import corpus_store
    from ingestion import rw_gazette as RG

    lines = [{"text": "Article One: Purpose", "x0": 300, "y0": 90, "x1": 420, "y1": 100, "page": 3}]
    reading_json = RG.reading(RG.structure(lines, "en"), {}, "0" * 64, {}, {3: (842.0, 595.0)})
    document = {
        "id": "rw-law-009-2026-en",
        "type": "law",
        "title": "Law n° 009/2026",
        "jurisdiction": "rw",
        "language": "en",
        "work": "rw-law-009-2026",
        "source_object": "sources/rw/minijust-gazette/abc.pdf",
        "objects": "files/abc/",
    }
    service = BlobService()
    held = service.store("corpus")
    manifest = json.dumps({"collection": "rw", "documents": [document]}).encode()
    held.put("collections/rw/manifest.json", manifest, "application/json")
    held.put(
        "collections/rw/files/abc/rw-law-009-2026-en.reading.json",
        json.dumps(reading_json).encode(),
        "application/json",
    )
    held.put("sources/rw/minijust-gazette/abc.pdf", b"%PDF-1.4 the issue", "application/pdf")
    opened = []

    def open_store(address):
        opened.append(address)
        return service.store("corpus")

    monkeypatch.setattr(content, "_STORES", {})
    monkeypatch.setattr(corpus_store, "open_store", open_store)
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", "az://oatest/corpus/collections/rw/")
    monkeypatch.setenv("ALVARY_REVIEW_CACHE", str(tmp_path / "cache"))
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    library = content.Library(tmp_path)
    assert "rw-law-009-2026-en" in [d["id"] for d in library.documents()]
    assert library.reading("rw-law-009-2026-en")["pages"][0]["page"] == 3
    assert library.original("rw-law-009-2026-en").read_bytes() == b"%PDF-1.4 the issue"
    assert library.act("rw-law-009-2026-en")["work"] == "rw-law-009-2026"  # it has no details beside it
    assert opened == ["az://oatest/corpus"]  # one store for the container
    cached = tmp_path / "cache" / "az" / "oatest" / "corpus" / "collections" / "rw"
    assert (cached / "sources" / "abc.pdf").is_file()
    assert (cached / "files" / "abc" / "rw-law-009-2026-en.reading.json").is_file()
    service.requests.clear()
    library.reading("rw-law-009-2026-en")
    library.original("rw-law-009-2026-en")
    assert service.requests == []  # read from the cache after the first time


def test_a_scanned_document_says_it_was_read_by_ocr_and_checked_by_a_second_reading():
    from workbench import content as wb_content

    def named(checks, pages):
        reading = {
            "primary": {"reader": "text-layer"},
            "checks": checks,
            "pages": [{"page": n} for n in pages],
        }
        return wb_content.reader_names(reading)

    scan = named(
        {"alignment": {}, "scans_read_by_ocr": [9, 10, 30], "scans_read_by_docai": [9, 10, 30]}, [9, 10]
    )
    assert scan["primary"] == "Google's OCR of the scanned pages"
    assert "is a scan" in scan["notes"][0] and "read a second time" in scan["notes"][-1]
    born_digital = named({"alignment": {"aligned": True}, "scans_read_by_ocr": [30]}, [9, 10])
    assert born_digital["primary"] == "the PDF's own text" and len(born_digital["notes"]) == 2


def test_the_document_list_is_searched_filtered_and_paged_on_the_server(tmp_path, monkeypatch):
    """A corpus collection lists tens of thousands of documents: an untouched one is summarised from
    its manifest entry alone (no reading opened), and the list comes a page at a time."""
    documents = [
        {
            "id": f"rw-law-{n:03d}-2026-{lang}",
            "type": "law",
            "title": f"Law n° {n:03d}/2026 on {'water' if n % 2 else 'roads'}",
            "language": lang,
            "work": f"rw-law-{n:03d}-2026",
            "page_count": 4,
            "flag_counts": {"high": n % 3, "medium": 0, "low": 0},
        }
        for n in range(1, 31)
        for lang in ("en", "fr")
    ]
    folder = tmp_path / "rw"
    folder.mkdir()
    (folder / "manifest.json").write_text(
        json.dumps({"collection": "rw", "jurisdiction": "rw", "documents": documents})
    )
    for d in documents:  # the readings exist, but the list must not open them
        (folder / f"{d['id']}.reading.json").write_text("not read by the list")
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", str(folder))
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    reviewer = signed_in((store, content.Library(tmp_path)), "reviewer@example.org")
    page = reviewer.get("/api/documents?language=fr&q=water&sort=title&limit=5").json()
    assert page["total"] == 15 and len(page["documents"]) == 5
    assert page["documents"][0]["id"] == "rw-law-001-2026-fr" and page["documents"][0]["pages"] == 4
    assert page["documents"][0]["status"] == "Not started"
    rest = reviewer.get("/api/documents?language=fr&q=water&sort=title&limit=5&offset=10").json()
    assert [d["id"] for d in rest["documents"]][-1] == "rw-law-029-2026-fr"
    overview = reviewer.get("/api/overview").json()
    assert overview["totals"]["documents"] == 61 and overview["languages"] == ["en", "fr"]


def test_a_deployed_workbench_renders_pages_itself_once_per_source_and_keeps_its_cache_bounded(
    tmp_path, monkeypatch
):
    import os
    import subprocess
    from pathlib import Path

    documents = [
        {
            "id": f"rw-law-009-2026-{lang}",
            "type": "law",
            "title": "Law",
            "work": "rw-law-009-2026",
            "source_object": "sources/rw/minijust-gazette/abc.pdf",
            "page_count": 1,
        }
        for lang in ("en", "fr")
    ]
    objects = {
        "collections/rw/manifest.json": json.dumps({"collection": "rw", "documents": documents}).encode(),
        "sources/rw/minijust-gazette/abc.pdf": b"%PDF-1.4 the issue",
    }
    asked, ran = [], []

    class Bucket:
        def get(self, name):
            asked.append(name)
            return objects[name]

        def exists(self, name):
            return name in objects

    def pdftoppm(command, **kwargs):
        ran.append(command)
        Path(command[-1] + ".jpg").write_bytes(b"\xff\xd8 image")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(content, "_STORES", {"test-bucket": Bucket()})
    monkeypatch.setattr(content.subprocess, "run", pdftoppm)
    monkeypatch.setenv("ALVARY_REVIEW_COLLECTIONS", "gs://test-bucket/collections/rw/")
    monkeypatch.setenv("ALVARY_REVIEW_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ALVARY_RENDER", "direct")
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    library = content.Library(tmp_path)
    english = library.image("rw-law-009-2026-en", 3)
    french = library.image("rw-law-009-2026-fr", 3)
    assert english == french and english.read_bytes() == b"\xff\xd8 image"
    assert len(ran) == 1 and ran[0][0] == "pdftoppm" and "docker" not in ran[0]
    assert asked.count("sources/rw/minijust-gazette/abc.pdf") == 1  # one copy of the issue for both
    # A cache past its budget drops what was used least recently.
    old = tmp_path / "cache" / "old.bin"
    old.write_bytes(b"x" * 1_500_000)
    os.utime(old, (1, 1))
    monkeypatch.setenv("ALVARY_REVIEW_CACHE_MB", "1")
    content.trim_cache()
    assert not old.exists() and english.exists()


def test_flags_at_one_place_with_no_words_of_their_own_each_have_their_own_id():
    flags = [
        {
            "severity": "high",
            "kind": "not_aligned",
            "primary": "",
            "tokens": [0, 0],
            "checks": {"French": f"Article {n}"},
        }
        for n in (26, 27, 28)
    ] + [{"severity": "medium", "kind": "reading_differs", "primary": "x", "tokens": [3, 4], "checks": {}}]
    ids = [f["id"] for f in content.page_flags({"page": 14, "flags": flags})]
    assert len(set(ids)) == 4
    # A flag that was already distinct keeps the id it had (decisions recorded on it stay valid).
    assert ids[3] == content.flag_id(14, flags[3])


def test_reading_a_whole_document_counts_its_pages_from_one_whatever_their_printed_numbers(
    tmp_path, monkeypatch
):
    rwanda_collection(tmp_path / "rw", monkeypatch)
    (tmp_path / f"{ACT}.reading.json").write_text(json.dumps(reading()))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    reviewer = signed_in((store, content.Library(tmp_path)), "reviewer@example.org")
    first = reviewer.get("/api/documents/rw-law-009-2026-fr/stream?start=1&count=6").json()
    assert [p["page"] for p in first["pages"]] == [3] and first["total"] == 1  # printed as page 3


def test_a_document_says_where_it_stands_and_opens_the_laws_that_repealed_or_were_made_under_it(
    tmp_path, monkeypatch
):
    folder = tmp_path / "rw"
    rwanda_collection(folder, monkeypatch)
    manifest = json.loads((folder / "manifest.json").read_text())
    later = {"work": "rw-law-010-2027", "title": "Law n° 010/2027 of 01/02/2027 governing examples"}
    order = {"work": "rw-presidential-order-001-01-2026", "title": "Presidential Order n° 001/01 of 2026"}
    for work in (later, order):
        (folder / f"{work['work']}-en.reading.json").write_text(
            (folder / "rw-law-009-2026-en.reading.json").read_text()
        )
        manifest["documents"].append(
            {"id": f"{work['work']}-en", "type": "law", "title": work["title"], "year": 2027, "jurisdiction": "rw",
             "language": "en", "work": work["work"]}
        )  # fmt: skip
    for document in manifest["documents"][:2]:
        document["relations"] = [
            {"kind": "repealed_by", "target": later["title"], "target_id": later["work"]}
        ]
        document["currency"] = {"status": "repealed", "basis": "text", "repealed_by": [later],
                                "disagrees": "RLRC's listing files it among the laws in force"}  # fmt: skip
        document["enables"] = [order["work"]]
    (folder / "manifest.json").write_text(json.dumps(manifest))
    store = Store(connect(f"sqlite:///{tmp_path / 'workbench.db'}"))
    store.add_operator("reviewer@example.org", "Rev", {"reviewer"})
    reviewer = signed_in((store, content.Library(tmp_path)), "reviewer@example.org")
    standing = reviewer.get("/api/documents/rw-law-009-2026-fr").json()["standing"]
    assert standing["status"] == "repealed" and standing["disagrees"].startswith("RLRC")
    assert standing["repealed_by"] == [
        {"title": later["title"], "id": "rw-law-010-2027-en"}
    ]  # no French text
    assert standing["enables"] == {
        "total": 1,
        "shown": [{"id": f"{order['work']}-en", "title": order["title"]}],
    }
    details = reviewer.get("/api/documents/rw-law-009-2026-en/details").json()
    # Suggested as repealed, never filled in: its status is not yet checked, its links not yet given.
    assert details["record"]["details"]["status.legal"] == "unknown"
    assert "relations" not in details["record"]["details"]
    assert details["suggestions"]["status.legal"]["value"] == "repealed"
    assert details["suggestions"]["status.legal"]["evidence"].startswith(f"Repealed by {later['title']}")
    assert details["related"] == {later["work"]: "rw-law-010-2027-en"}
    assert details["suggestions"]["relations"]["value"][0]["kind"] == "repealed_by"
    assert reviewer.get("/api/documents/rw-law-010-2027-en").json()["standing"] is None  # not linked yet


# --- the text publishing will write (TXP-01) ----------------------------------------------------------


def test_the_document_shows_its_text_and_structure_and_the_approval_binds_them(world):
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    check = approver.get(f"/api/documents/{ACT}").json()["text_check"]
    assert check["profile"] == "oa-text-1/workbench-units-1" and check["rules"] == "ng-federal-act-1"
    assert check["counts"] == {"section": 2} and check["errors"] == []
    published = approver.get(f"/api/documents/{ACT}/canonical").json()
    assert (
        published["text"]
        == "Accounts\n\n1. Accounts prepared under section 199 of this Act.\n\n2. Citation.\n"
    )
    sections = [n for n in published["nodes"] if n["kind"] == "section"]
    assert [(n["locator"], n["heading"]) for n in sections] == [("sec_1", "Accounts"), ("sec_2", "")]
    confirm_details(approver)
    assert approve(approver).json() == {"revision": 1}
    record = approver.get(f"/api/documents/{ACT}").json()["approvals"][0]
    assert record["text_sha256"] == published["text_sha256"] == check["text_sha256"]
    assert record["structure_sha256"] == published["structure_sha256"]
    assert (record["serialization_profile"], record["structure_rules"]) == (check["profile"], check["rules"])


def test_a_refused_page_break_join_blocks_approval(world):
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    joined = [{"kind": "paragraph", "text": "2. Citation.", "continues_previous": True}]
    assert save(approver, page=2, units=joined).status_code == 200  # numbered: the join is refused
    confirm_details(approver)
    refused = approve(approver)
    assert refused.status_code == 409 and refused.json()["readiness"]["text"]["errors"] == 1
    errors = approver.get(f"/api/documents/{ACT}").json()["text_check"]["errors"]
    assert [(e["code"], e["page"]) for e in errors] == [("join", 2)]
    save(approver, page=2, expected=1, units=[{"kind": "paragraph", "text": "2. Citation."}])
    assert approve(approver).json() == {"revision": 1}


FINES = {
    "kind": "table",
    "caption": "",
    "header_rows": 1,
    "rows": [["Offence", "Fine"], ["Hacking", ""]],
    "cell_states": {"1,1": "illegible"},
}


def test_marked_table_cells_are_validated_and_shown(world):
    from workbench import akn

    assert content.validate_units([FINES]) == [FINES]
    assert "cell_states" not in content.validate_units([{**FINES, "cell_states": {}}])[0]
    for wrong in (
        {"cell_states": {"1,2": "illegible"}},  # no such cell
        {"cell_states": {"01,1": "illegible"}},  # not how a cell is named
        {"cell_states": {"1,1": "blank"}},  # not a state
        {"cell_states": [["1,1", "illegible"]]},
        {"spans": [[1, 0, 1, 2]]},  # covered by the merged "Hacking"
    ):
        with pytest.raises(ValueError):
            content.validate_units([{**FINES, **wrong}])
    reviewer = signed_in(world, "reviewer@example.org")
    assert save(reviewer, units=[FINES]).status_code == 200
    assert reviewer.get(f"/api/documents/{ACT}/pages/1").json()["latest"]["units"] == [FINES]
    block = reviewer.get(f"/api/documents/{ACT}/public-preview").json()["pages"][0]["blocks"][0]
    assert block["cell_states"] == FINES["cell_states"]
    assert "Offence\tFine\nHacking\t[illegible]\n" in reviewer.get(f"/api/documents/{ACT}/text").text
    cell = akn.table(FINES).findall(f"{{{akn.NS}}}tr")[1].findall(f"{{{akn.NS}}}td")[1]
    assert (cell.get("class"), cell.get("title"), "".join(cell.itertext())) == (
        "illegible",
        "Illegible in the original",
        "",
    )


def test_a_marked_cell_in_an_operative_table_blocks_approval_until_it_is_filled(world):
    approver = signed_in(world, "approver@example.org")
    units = [{"kind": "paragraph", "text": "2. Fines."}, FINES]
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    assert save(approver, page=2, units=units).status_code == 200
    check = approver.get(f"/api/documents/{ACT}").json()["text_check"]
    assert check["profile"] == "oa-text-1/workbench-units-2"
    assert [(e["code"], e["page"]) for e in check["errors"]] == [("incomplete_table", 2)]
    confirm_details(approver)
    refused = approve(approver)
    assert refused.status_code == 409 and refused.json()["readiness"]["text"]["errors"] == 1
    # Read from a better copy: the fine is filled in and the mark cleared.
    filled = {k: v for k, v in FINES.items() if k != "cell_states"} | {
        "rows": [["Offence", "Fine"], ["Hacking", "N5m"]]
    }
    save(approver, page=2, expected=1, units=[units[0], filled])
    assert (
        approver.get(f"/api/documents/{ACT}").json()["text_check"]["profile"] == "oa-text-1/workbench-units-1"
    )
    assert approve(approver).json() == {"revision": 1}


def test_a_page_says_how_the_page_before_it_ends(world):
    reviewer = signed_in(world, "reviewer@example.org")
    assert reviewer.get(f"/api/documents/{ACT}/pages/1").json()["previous_end"] is None
    before = reviewer.get(f"/api/documents/{ACT}/pages/2").json()["previous_end"]
    assert before == {
        "page": 1,
        "text": "1. Accounts prepared under section 198 of this Act.",
        "hyphen": False,
        "ends_sentence": True,
    }
    save(reviewer, units=[{"kind": "paragraph", "text": "1. Accounts prepared under sec-"}])
    before = reviewer.get(f"/api/documents/{ACT}/pages/2").json()["previous_end"]
    assert before["hyphen"] and not before["ends_sentence"]


def test_join_marks_are_validated_and_words_doubled_in_editing_are_reported(world):
    reviewer = signed_in(world, "reviewer@example.org")
    hyphen_only = [{"kind": "paragraph", "text": "x", "dehyphenate": True}]
    assert save(reviewer, page=2, units=hyphen_only).status_code == 422
    not_boolean = [{"kind": "paragraph", "text": "x", "continues_previous": "yes"}]
    assert save(reviewer, page=2, units=not_boolean).status_code == 422
    save(reviewer, units=[{"kind": "paragraph", "text": "1. Accounts prepared prepared under section 199."}])
    warnings = reviewer.get(f"/api/documents/{ACT}").json()["text_check"]["warnings"]
    assert [(w["code"], w["page"]) for w in warnings] == [("doubled_word", 1)]


def test_the_first_collection_can_live_in_the_store_and_is_mirrored_whole(tmp_path, monkeypatch):
    from blob_support import BlobService

    from ingestion import corpus_store

    service = BlobService()
    held = service.store("corpus")
    held.put(f"collections/ng/{ACT}.reading.json", json.dumps(reading()).encode(), "application/json")
    held.put(f"collections/ng/review-images/{ACT}/page-1.jpg", b"jpeg bytes", "image/jpeg")
    monkeypatch.setattr(content, "_STORES", {})
    monkeypatch.setattr(corpus_store, "open_store", lambda address: service.store("corpus"))
    monkeypatch.setenv("ALVARY_REVIEW_STAGING", "az://oatest/corpus/collections/ng/")
    monkeypatch.setenv("ALVARY_REVIEW_CACHE", str(tmp_path / "cache"))
    library = content.Library()
    assert (library.staging / f"{ACT}.reading.json").is_file()
    assert (library.staging / "review-images" / ACT / "page-1.jpg").read_bytes() == b"jpeg bytes"
    assert ACT in [d["id"] for d in library.documents()]
    # The next start fetches the readings again (they may have been re-read), not the images.
    service.requests.clear()
    content.Library()
    fetched = [r.url.path for r in service.requests if r.method == "GET" and "comp=list" not in str(r.url)]
    assert fetched == [f"/corpus/collections/ng/{ACT}.reading.json"]
    # Trimming the cache takes what other collections fetched, never the first collection.
    other = tmp_path / "cache" / "az" / "oatest" / "corpus" / "big.pdf"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x" * 2 * 1024 * 1024)
    monkeypatch.setenv("ALVARY_REVIEW_CACHE_MB", "1")
    content.trim_cache()
    assert not other.exists()
    assert (library.staging / "review-images" / ACT / "page-1.jpg").is_file()
