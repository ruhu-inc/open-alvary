import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from workbench import auth, content
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


def client(world, oidc=None, dev=True, host="127.0.0.1"):
    store, library = world
    return TestClient(create_app(store, library, oidc, dev_login=dev), base_url=f"http://{host}")


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
        f"/api/acts/{ACT}/pages/{page}",
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
        "/api/acts",
        f"/api/acts/{ACT}",
        f"/api/acts/{ACT}/pages/1",
        f"/api/acts/{ACT}/pages/1/image",
        f"/api/acts/{ACT}/export",
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
        f"/api/acts/{ACT}/approval", json={"expected_revision": 0, "decision": "approved"}
    )
    assert refused.status_code == 403 and "approver" in refused.json()["message"]


def test_a_deactivated_operator_and_a_signed_out_session_lose_access(world):
    store, _ = world
    browser = signed_in(world, "reviewer@example.org")
    store.deactivate("reviewer@example.org")
    assert browser.get("/api/acts").status_code == 401
    other = signed_in(world, "approver@example.org")
    assert other.post("/auth/logout").status_code == 200
    assert other.get("/api/acts").status_code == 401


def test_an_idle_session_expires(world):
    store, _ = world
    browser = signed_in(world, "reviewer@example.org")
    with store.engine.begin() as db:
        db.execute(update(session).values(last_seen=store_now_minus(hours=2)))
    assert browser.get("/api/acts").status_code == 401


def store_now_minus(hours):
    from datetime import timedelta

    from workbench.store import now

    return now() - timedelta(hours=hours)


# --- review ------------------------------------------------------------------------------------------


def test_page_detail_offers_units_flags_and_image(world):
    browser = signed_in(world, "reviewer@example.org")
    page = browser.get(f"/api/acts/{ACT}/pages/1").json()
    assert page["machine_units"] == [
        {
            "kind": "paragraph",
            "text": "1. Accounts prepared under section 198 of this Act.",
            "notes": ["Accounts"],
        }
    ]
    assert page["flags"][0]["id"] == flag_id() and page["latest"] is None
    assert browser.get(f"/api/acts/{ACT}/pages/1/image").headers["content-type"] == "image/jpeg"
    assert browser.get(f"/api/acts/{ACT}/pages/9").status_code == 404
    assert browser.get("/api/acts/ng-unknown/pages/1").status_code == 404


def test_saving_is_versioned_and_a_stale_save_conflicts(world):
    first = signed_in(world, "reviewer@example.org")
    second = signed_in(world, "approver@example.org")
    assert save(first).json() == {"revision": 1}
    conflict = save(second, expected=0)
    assert conflict.status_code == 409
    assert save(second, expected=1, status="attention").json() == {"revision": 2}
    history = first.get(f"/api/acts/{ACT}/pages/1").json()["history"]
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


def approve(browser, expected=0, **overrides):
    body = {
        "expected_revision": expected,
        "decision": "approved",
        "rights_basis": "s.3(b)",
        "materials": MATERIALS,
        **overrides,
    }
    return browser.post(f"/api/acts/{ACT}/approval", json=body)


def test_approval_waits_for_every_page_and_high_flag_and_binds_the_content(world):
    approver = signed_in(world, "approver@example.org")
    refused = approve(approver)
    assert refused.status_code == 409 and refused.json()["readiness"]["unchecked_pages"] == [1, 2]
    save(approver)  # page 1 checked, but its high flag is not resolved
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    assert approve(approver).json()["readiness"]["open_high_flags"] == [{"page": 1, "id": flag_id()}]
    save(approver, expected=1, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    assert approve(approver, materials={**MATERIALS, "dates": False}).status_code == 422
    assert approve(approver, rights_basis=" ").status_code == 422
    assert approve(approver).json() == {"revision": 1}
    exported = approver.get(f"/api/acts/{ACT}/export").json()
    assert exported["approved"] and exported["pages"][0]["units"][0]["text"].endswith(
        "section 199 of this Act."
    )
    assert "[Margin note: Accounts]" in approver.get(f"/api/acts/{ACT}/text").text
    save(
        approver,
        expected=2,
        units=[{"kind": "paragraph", "text": "1. Accounts prepared under section 199 of this Act!"}],
        resolutions={flag_id(): {"decision": "edited"}},
    )
    assert (
        approver.get(f"/api/acts/{ACT}/export").json()["approved"] is False
    )  # an edit after approval voids it
    listed = approver.get("/api/acts").json()[0]
    assert listed["approval"] == "approved" and listed["approval_current"] is False
    actions = [e["action"] for e in approver.get(f"/api/audit?act={ACT}").json()]
    assert "act_approved" in actions and actions.count("page_saved") == 4


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
    browser = client(world, oidc=oidc, dev=False, host="review.example.org")
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
    assert browser.get(f"/auth/callback?state={state}&code=abc").status_code == 400  # one use only


@pytest.mark.parametrize(
    ("claims", "status"),
    [
        ({"aud": "someone-else"}, 401),
        ({"iss": "https://evil.example.org"}, 401),
        ({"nonce": "replayed"}, 401),
        ({"exp": time.time() - 10}, 401),
        ({"email_verified": False}, 401),
        ({"email": "stranger@example.org"}, 403),
    ],
)
def test_oidc_sign_in_rejects_bad_claims_and_strangers(world, claims, status):
    browser, state = oidc_flow(world, **claims)
    response = browser.get(f"/auth/callback?state={state}&code=abc")
    assert response.status_code == status
    assert "set-cookie" not in response.headers


# --- document surface support and Akoma Ntoso --------------------------------------------------------


def test_unit_levels_are_kept_and_bounded(world):
    browser = signed_in(world, "reviewer@example.org")
    assert save(browser, units=[{"kind": "paragraph", "text": "(a) any law", "level": 2}]).status_code == 200
    assert browser.get(f"/api/acts/{ACT}/pages/1").json()["latest"]["units"][0]["level"] == 2
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
    region = browser.get(f"/api/acts/{ACT}/pages/1").json()["regions"][0]
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
    response = browser.get(f"/api/acts/{ACT}/akn")
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
    found = browser.get(f"/api/acts/{ACT}/search", params={"q": "section"}).json()
    assert [(h["page"], h["match"]) for h in found["hits"]] == [(1, "section")]
    assert browser.get(f"/api/acts/{ACT}/search", params={"q": "Sect", "case": True}).json()["hits"] == []
    assert browser.get(f"/api/acts/{ACT}/search", params={"q": "sect", "word": True}).json()["hits"] == []
    assert browser.get(f"/api/acts/{ACT}/search", params={"q": ""}).status_code == 422
    result = browser.post(
        f"/api/acts/{ACT}/replace",
        json={
            "find": "198",
            "replace": "199",
            "word": True,
            "pages": {str(p): r for p, r in found["revisions"].items()},
        },
    ).json()
    assert result == {"changed": [{"page": 1, "revision": 1, "replacements": 1}], "conflicts": []}
    page = browser.get(f"/api/acts/{ACT}/pages/1").json()["latest"]
    assert page["units"][0]["text"].endswith("section 199 of this Act.") and page["status"] == "edited"
    assert (
        page["author"] == "reviewer@example.org"
        and "Replaced '198' with '199'"
        in browser.get(f"/api/acts/{ACT}/pages/1").json()["history"][0]["comment"]
    )
    stale = browser.post(
        f"/api/acts/{ACT}/replace", json={"find": "Act", "replace": "ACT", "pages": {"1": 0}}
    ).json()
    assert stale["conflicts"] == [1]  # the preview was older than the page


def test_the_stream_returns_final_content_for_continuous_reading(world):
    browser = signed_in(world, "reviewer@example.org")
    stream = browser.get(f"/api/acts/{ACT}/stream", params={"start": 1, "count": 5}).json()
    assert stream["total"] == 2 and [p["page"] for p in stream["pages"]] == [1, 2]
    assert stream["pages"][0]["open_high"] == 1 and stream["pages"][0]["units"][0]["notes"] == ["Accounts"]
