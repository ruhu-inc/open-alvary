"""The review workbench web application (operator only).

    ALVARY_REVIEW_DATABASE_URL=... uvicorn workbench.app:app --host 127.0.0.1 --port 8090

Put it on the web only behind TLS (a reverse proxy) with ALVARY_REVIEW_BASE_URL set to its public
https address and an OpenID Connect client configured; see the runbook.
"""

import hmac
import html
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ingestion import settings
from schema import record as R
from schema.jurisdictions import registry as jurisdiction_registry
from workbench import access as access_section
from workbench import akn, auth, content, details, privileged, publication, record_view, serialize
from workbench import operators as operators_section
from workbench.review import Review, blind, to_do
from workbench.store import Conflict, Store, engine_from_environment

STATIC = Path(__file__).with_name("static")
DECISIONS = {"keep", "use_check", "edited", "dismissed"}
SENTENCE_END = re.compile(r"[.;:!?—–]['’”\")\]]*$")  # a page ending here finishes its paragraph
JSON_BODY = Body(...)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def create_app(
    store: Store,
    library: content.Library,
    oidc: auth.OIDC | None = None,
    dev_login: bool = False,
    access=None,
    relying=None,
) -> FastAPI:
    """`access` is the Access section's accounts connection (accounts.access_admin.AccessAdmin);
    `relying` binds passkeys to the workbench's address (workbench.passkeys.RelyingParty)."""
    app = FastAPI(title="Open Alvary review workbench", docs_url=None, redoc_url=None, openapi_url=None)
    hosts = [h.strip() for h in os.environ.get("ALVARY_REVIEW_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # --- sign-in ---------------------------------------------------------------------------------

    @app.get("/auth/login")
    def login(request: Request):
        if not oidc:
            raise HTTPException(404, "Sign-in with an identity provider is not configured")
        url = oidc.authorize_url(store)
        response = RedirectResponse(url, status_code=303)
        # The sign-in must finish in the browser that started it: the flow's state goes with that
        # browser in a short-lived cookie, and the callback is refused without it (a callback
        # link carried to another browser cannot sign that browser in).
        state = parse_qs(urlsplit(url).query)["state"][0]
        response.set_cookie(
            auth.FLOW_COOKIE,
            state,
            httponly=True,
            samesite="lax",  # sent on the provider's top-level redirect back to us
            secure=not auth.is_loopback(request),
            path="/auth",
            max_age=600,
        )
        return response

    def back_to_sign_in(reason: str) -> RedirectResponse:
        """A failed sign-in goes back to the sign-in page, which says why from a fixed list."""
        response = RedirectResponse(f"/?signin={reason}", status_code=303)
        response.delete_cookie(auth.FLOW_COOKIE, path="/auth")
        return response

    @app.get("/auth/callback")
    def callback(request: Request, state: str = "", code: str = ""):
        started = request.cookies.get(auth.FLOW_COOKIE, "")
        if oidc and state and not (started and hmac.compare_digest(started, state)):
            return back_to_sign_in("other_browser")
        if not oidc or not state or not code:
            return back_to_sign_in("failed")
        try:
            email = oidc.finish(store, state, code)
        except HTTPException as error:
            detail = str(error.detail)
            reason = (
                "not_operator"
                if error.status_code == 403
                else "unverified"
                if "not verified" in detail
                else "expired"
                if "xpired" in detail or "already used" in detail
                else "failed"
            )
            return back_to_sign_in(reason)
        token, _ = store.start_session(email)
        # A same-site navigation home, so the strict session cookie is sent with it.
        response = HTMLResponse(
            "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0;url=/'>"
            "<p>Signed in. <a href='/'>Continue</a></p>"
        )
        response.set_cookie(auth.COOKIE, token, **auth.cookie_options(request))
        response.delete_cookie(auth.FLOW_COOKIE, path="/auth")
        return response

    @app.post("/auth/dev")
    def dev(request: Request, body: dict = JSON_BODY):
        if not dev_login or not auth.is_loopback(request):
            raise HTTPException(404, "Not available")
        email = str(body.get("email", "")).strip().lower()
        if not store.operator(email):
            raise HTTPException(403, "Not an operator")
        token, _ = store.start_session(email)
        response = JSONResponse({"ok": True})
        response.set_cookie(auth.COOKIE, token, **auth.cookie_options(request))
        return response

    @app.post("/auth/logout")
    def logout(request: Request):
        auth.current(request, store, write=True)
        store.end_session(request.cookies[auth.COOKIE])
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    @app.get("/api/me")
    def me(request: Request):
        try:
            who = auth.current(request, store)
        except HTTPException:
            return JSONResponse(
                {
                    "signed_in": False,
                    "oidc": bool(oidc),
                    "dev_login": dev_login and auth.is_loopback(request),
                },
                status_code=401,
            )
        person = who["operator"]
        return {
            "signed_in": True,
            "email": who["email"],
            "name": person["name"],
            "roles": sorted(person["roles"]),
            "csrf": who["csrf"],
            "access": access is not None and relying is not None and access_section.ROLE in person["roles"],
            "operators": relying is not None and operators_section.ROLE in person["roles"],
        }

    # --- review ----------------------------------------------------------------------------------

    def act_or_404(act_id: str) -> dict:
        try:
            return library.act(act_id)
        except KeyError as error:
            raise HTTPException(404, "No such document") from error

    def reading_page(act_id: str, page: int) -> tuple[dict, dict]:
        reading = library.reading(act_id)
        found = next((p for p in reading["pages"] if p["page"] == page), None)
        if not found:
            raise HTTPException(404, "No such page")
        return reading, found

    def previous_end(act_id: str, reading: dict, page: int) -> dict | None:
        """How the page before ends, as now reviewed: whether this page's first paragraph may
        continue it (TXP-02 rule 8)."""
        numbers = [p["page"] for p in reading["pages"]]
        if page not in numbers or numbers.index(page) == 0:
            return None
        before = reading["pages"][numbers.index(page) - 1]
        revision = store.latest_pages(act_id).get(before["page"])
        units = revision["units"] if revision else content.units_from(before)
        words = next(
            (w for u in reversed(units) if u.get("kind") != "table" and (w := serialize.unit_words(u))), ""
        )
        if not words:
            return None
        return {
            "page": before["page"],
            "text": words[-160:],
            "hyphen": words.endswith("-"),
            "ends_sentence": bool(SENTENCE_END.search(words)),
        }

    profiles = jurisdiction_registry()
    review = Review(store, library, profiles)
    details_key, described, built, state = review.details_key, review.described, review.built, review.state

    def document_summary(document: dict) -> dict:
        now = state(library.act(document["id"]))
        reading, latest, ready, head = now["reading"], now["latest"], now["readiness"], now["head"]
        last = store.events(act_id=document["id"], limit=1)
        approval = None
        if head and head["decision"] == "approved":
            approval = "approved" if now["approved"] else "out_of_date"
        return with_status(
            {
                **listed(document),
                "pages": ready["pages"],
                "checked": ready["pages"] - len({*ready["unchecked_pages"], *ready["unresolved_flag_pages"]}),
                "attention": sum(1 for r in latest.values() if r["status"] == "attention"),
                "open_high": len(ready["open_high_flags"]),
                "stale": len(ready["stale_pages"]),
                "ready": ready["ready"],
                "approval": approval,
                "last": {"at": last[0]["at"].isoformat(), "actor": last[0]["actor"], "page": last[0]["page"]}
                if last
                else None,
                "_latest": latest,
                "_reading": reading,
            }
        )

    def listed(document: dict) -> dict:
        return {
            "id": document["id"],
            "title": document["title"],
            "type": document.get("type", "act"),
            "year": document.get("year"),
            "jurisdiction": document.get("jurisdiction"),
            "language": document.get("language"),
        }

    def recorded(approval: dict) -> dict:
        """An approval as the workbench shows it: without the routing plan it bound, whose audit sample
        reviewers must not see (CPR-01: audit pages are reviewed blind)."""
        shown = {k: v for k, v in approval.items() if k != "routing"}
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in shown.items()}

    def with_status(row: dict) -> dict:
        if row["approval"] == "approved":
            status = "Approved"
        elif row["approval"] == "out_of_date":
            status = "Approval out of date"
        elif row["ready"]:
            status = "Ready for approval"
        elif not row["checked"] and not row["attention"] and not row["last"]:
            status = "Not started"
        else:
            status = "In review"
        return {**row, "status": status}

    def untouched(document: dict) -> dict:
        """A document nobody has worked on, from its manifest entry alone: its machine reading's page
        and flag counts, nothing checked (no reading is opened: there are tens of thousands)."""
        flags = document.get("flag_counts") or {}
        return with_status(
            {
                **listed(document),
                "pages": document["page_count"],
                "checked": 0,
                "attention": 0,
                "open_high": flags.get("high", 0),
                "stale": 0,
                "ready": False,
                "approval": None,
                "last": None,
            }
        )

    # Each worked-on document's summary, kept until its latest event or details revision changes.
    summary_cache: dict[str, tuple[tuple, dict]] = {}

    def summaries() -> list[dict]:
        acts, details = store.activity_stamps(), store.detail_stamps()
        rows = []
        for document in library.documents():
            stamp = (acts.get(document["id"]), details.get(details_key(document)))
            if stamp == (None, None) and document.get("page_count") is not None:
                rows.append(untouched(document))
                continue
            cached = summary_cache.get(document["id"])
            if cached is None or cached[0] != stamp or stamp == (None, None):
                row = document_summary(document)
                cached = (stamp, {k: v for k, v in row.items() if not k.startswith("_")})
                summary_cache[document["id"]] = cached
            rows.append(cached[1])
        return rows

    SORTS = {
        "recent": lambda r: r["last"]["at"] if r["last"] else "",
        "title": lambda r: r["title"].lower(),
        "flags": lambda r: -r["open_high"],
        "progress": lambda r: r["checked"] / r["pages"] if r["pages"] else 0,
    }

    @app.get("/api/overview")
    def overview(request: Request):
        """What the home page shows above the list: totals, what needs attention, where to continue,
        recent activity, and the types and languages to filter by. The list itself is /api/documents."""
        who = auth.current(request, store)
        rows = summaries()
        resume = None
        last = store.last_event(who["email"], "page_saved")
        if last and last["act_id"] in {r["id"] for r in rows}:
            row = document_summary(library.act(last["act_id"]))
            pages = [p["page"] for p in row["_reading"]["pages"]]
            todo = [p for p in pages if (row["_latest"].get(p) or {}).get("status") != "checked"]
            after = [p for p in todo if p > (last["page"] or 0)] or todo
            resume = {
                "id": row["id"],
                "title": row["title"],
                "page": after[0] if after else last["page"],
                "last_page": last["page"],
                "at": last["at"].isoformat(),
            }
        titles = {r["id"]: r["title"] for r in rows if r["last"]}
        activity = [
            {
                "at": e["at"].isoformat(),
                "actor": e["actor"],
                "action": e["action"],
                "id": e["act_id"],
                "title": titles.get(e["act_id"]),
                "page": e["page"],
            }
            for e in store.events(limit=300)
            if e["action"] in {"page_saved", "details_saved", "act_approved", "act_revoked"}
        ][:60]
        attention = [
            r
            for r in rows
            if r["last"]
            and (r["open_high"] or r["attention"] or r["stale"] or r["approval"] == "out_of_date")
        ]
        return {
            "totals": {
                "documents": len(rows),
                "pages": sum(r["pages"] for r in rows),
                "checked": sum(r["checked"] for r in rows),
                "open_high": sum(r["open_high"] for r in rows),
                "approved": sum(1 for r in rows if r["approval"] == "approved"),
                "ready": sum(1 for r in rows if r["ready"] and r["approval"] != "approved"),
            },
            "attention": sorted(attention, key=lambda r: -r["open_high"])[:6],
            "types": sorted({r["type"] for r in rows}),
            "languages": sorted({r["language"] for r in rows if r["language"]}),
            "resume": resume,
            "activity": activity,
            # Operators by display name, for the activity and each document's last activity.
            "names": store.names(
                {e["actor"] for e in activity} | {r["last"]["actor"] for r in rows if r["last"]}
            ),
        }

    @app.get("/api/documents")
    def acts(
        request: Request,
        q: str = "",
        type: str = "",
        language: str = "",
        status: str = "",
        sort: str = "recent",
        offset: int = 0,
        limit: int = 50,
    ):
        """The documents, searched (title or id), filtered and sorted on the server, one page at a
        time: a corpus collection holds tens of thousands."""
        auth.current(request, store)
        words = q.lower().split()
        rows = [
            r
            for r in summaries()
            if all(w in r["title"].lower() or w in r["id"] for w in words)
            and (not type or r["type"] == type)
            and (not language or r["language"] == language)
            and (not status or r["status"] == status)
        ]
        rows.sort(key=SORTS["title"])
        if sort in SORTS and sort != "title":
            # Most recent activity first; documents nobody has touched after them, by title (a
            # stable sort keeps the title order among equals, reversed or not).
            rows.sort(key=SORTS[sort], reverse=sort == "recent")
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        return {
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "documents": rows[offset : offset + limit],
        }

    @app.get("/api/documents/{act_id}")
    def act_detail(request: Request, act_id: str):
        auth.current(request, store)
        act = act_or_404(act_id)
        now = state(act, text=True)
        reading, latest = now["reading"], now["latest"]
        todo = to_do(now["readiness"])  # audit pages are "review" pages, like full ones (CPR-01)
        pages = []
        for page in reading["pages"]:
            revision = latest.get(page["page"])
            counts = {
                level: sum(f["severity"] == level for f in page["flags"])
                for level in ("high", "medium", "low")
            }
            resolved = set(revision["resolutions"]) if revision else set()
            open_high = sum(
                1 for f in content.page_flags(page) if f["severity"] == "high" and f["id"] not in resolved
            )
            pages.append(
                {
                    "page": page["page"],
                    "flags": counts,
                    "open_high": open_high,
                    "revision": revision["revision"] if revision else 0,
                    "status": revision["status"] if revision else None,
                    "author": revision["author"] if revision else None,
                    "stale": bool(revision and revision["reading_sha256"] != content.reading_sha256(page)),
                    "todo": todo.get(page["page"]),
                }
            )
        provenance = library.provenance(act_id)
        return {
            "act": {**act, "rights_basis": library.manifest_for(act_id).get("rights_basis", "")},
            "standing": standing(act),
            "readers": {"primary": reading["primary"], "checks": reading["checks"]},
            "pages": pages,
            # Where to check the document against its source: the address it was fetched from, and
            # whether the original file itself can be opened here.
            "source": {
                "official_url": provenance.get("url") or act.get("url"),
                "original_pdf": library.has_original(act_id),
            },
            "readiness": blind(now["readiness"]),
            "text_check": publication.summary(now["text"]),
            "content_sha256": now["content_sha256"],
            "details_sha256": now["details_sha256"],
            "approved": now["approved"],
            "approvals": [recorded(a) for a in now["approvals"]],
            "materials": content.MATERIALS,
        }

    @app.get("/api/documents/{act_id}/canonical")
    def canonical(request: Request, act_id: str):
        """The exact text and nodes publishing would write from the pages as they now stand."""
        auth.current(request, store)
        return publication.canonical(state(act_or_404(act_id), text=True)["text"])

    @app.get("/api/documents/{act_id}/pages/{page}")
    def page_detail(request: Request, act_id: str, page: int):
        auth.current(request, store)
        act_or_404(act_id)
        reading, found = reading_page(act_id, page)
        history = store.page_history(act_id, page)
        latest = history[0] if history else None
        return {
            "page": page,
            "previous_end": previous_end(act_id, reading, page),
            "machine_units": content.units_from(found),
            "machine_html": content.machine_html(found),
            "flags": content.page_flags(found),
            "checks": found["checks"],
            # Boxes from the positional check reader, else those the reading carries on its page.
            "regions": content.regions(library.staging_for(act_id), act_id, page)
            or list(found.get("regions") or []),
            "lines": content.lines(library.staging_for(act_id), act_id, page)
            or list(found.get("lines") or []),
            "reading_sha256": content.reading_sha256(found),
            "latest": {
                "revision": latest["revision"],
                "units": latest["units"],
                "status": latest["status"],
                "resolutions": latest["resolutions"],
                "comment": latest["comment"],
                "author": latest["author"],
                "created_at": latest["created_at"].isoformat(),
                "stale": latest["reading_sha256"] != content.reading_sha256(found),
            }
            if latest
            else None,
            "history": [
                {
                    "revision": h["revision"],
                    "status": h["status"],
                    "author": h["author"],
                    "created_at": h["created_at"].isoformat(),
                    "comment": h["comment"],
                }
                for h in history
            ],
        }

    @app.get("/api/documents/{act_id}/pages/{page}/image")
    def page_image(request: Request, act_id: str, page: int):
        auth.current(request, store)
        act_or_404(act_id)
        reading_page(act_id, page)
        try:
            image = library.image(act_id, page)
        except FileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        return FileResponse(
            image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.post("/api/documents/{act_id}/pages/{page}")
    def save_page(request: Request, act_id: str, page: int, body: dict = JSON_BODY):
        who = auth.current(request, store, write=True, role="reviewer")
        act_or_404(act_id)
        _, found = reading_page(act_id, page)
        try:
            units = content.validate_units(body.get("units"))
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error
        status = body.get("status")
        if status not in {"checked", "attention", "edited"}:
            raise HTTPException(422, "status must be checked, attention or edited")
        known = {f["id"] for f in content.page_flags(found)}
        resolutions = body.get("resolutions") or {}
        if not isinstance(resolutions, dict) or not set(resolutions) <= known:
            raise HTTPException(422, "resolutions must refer to this page's flags")
        clean = {}
        for flag, value in resolutions.items():
            if not isinstance(value, dict) or value.get("decision") not in DECISIONS:
                raise HTTPException(422, f"decision must be one of {sorted(DECISIONS)}")
            clean[flag] = {
                "decision": value["decision"],
                "reader": str(value.get("reader", ""))[:20],
                "note": str(value.get("note", ""))[:500],
            }
        comment = str(body.get("comment", ""))[:2000]
        try:
            revision = store.save_page(
                act_id,
                page,
                int(body.get("expected_revision", -1)),
                who["email"],
                reading_sha256=content.reading_sha256(found),
                units=units,
                status=status,
                resolutions=clean,
                comment=comment,
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return {"revision": revision}

    @app.post("/api/documents/{act_id}/approval")
    def approve(request: Request, act_id: str, body: dict = JSON_BODY):
        who = auth.current(request, store, write=True, role="approver")
        now = state(act_or_404(act_id), text=True)
        decision = body.get("decision")
        if decision not in {"approved", "revoked"}:
            raise HTTPException(422, "decision must be approved or revoked")
        materials = body.get("materials") or {}
        basis = str(body.get("rights_basis", "")).strip()[:2000]
        if decision == "approved":
            ready = now["readiness"]
            if not ready["ready"]:
                raise HTTPException(409, {"message": "Not ready for approval", "readiness": ready})
            if set(materials) != set(content.MATERIALS) or not all(materials.values()):
                raise HTTPException(422, "Confirm every verification check")
            if not basis:
                raise HTTPException(422, "State the rights basis")
        note = str(body.get("note", ""))[:2000]
        try:
            revision = store.record_approval(
                act_id,
                int(body.get("expected_revision", -1)),
                who["email"],
                decision=decision,
                content_sha256=now["content_sha256"],
                details_sha256=now["details_sha256"],
                text_sha256=now["text"].text_sha256,
                structure_sha256=now["text"].structure_sha256,
                serialization_profile=now["text"].profile_id,
                structure_rules=now["text"].rules_id,
                rights_basis=basis,
                materials={k: bool(v) for k, v in materials.items() if k in content.MATERIALS},
                note=note,
                routing=now["readiness"].get("routing") if decision == "approved" else None,
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return {"revision": revision}

    @app.get("/api/documents/{act_id}/export")
    def export(request: Request, act_id: str):
        auth.current(request, store)
        act = act_or_404(act_id)
        now = state(act)
        head, record = now["head"], now["record"]
        return {
            "act": act,
            "input_sha256": now["reading"]["input_sha256"],
            "readers": {"primary": now["reading"]["primary"], "checks": now["reading"]["checks"]},
            "content_sha256": now["content_sha256"],
            "details_sha256": now["details_sha256"],
            "approved": now["approved"],
            "approval": recorded(head) if head else None,
            "details": {"details": record["details"], "absent": record["absent"], "status": record["status"]}
            if record
            else None,
            "pages": now["final"],
        }

    @app.get("/api/documents/{act_id}/search")
    def search(request: Request, act_id: str, q: str = "", case: bool = False, word: bool = False):
        auth.current(request, store)
        act_or_404(act_id)
        final = content.final_pages(library.reading(act_id), store.latest_pages(act_id))
        try:
            hits = content.search(final, q, case, word)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {
            "hits": hits,
            "revisions": {p["page"]: p["revision"] for p in final},
            "truncated": len(hits) >= 500,
        }

    @app.post("/api/documents/{act_id}/replace")
    def replace(request: Request, act_id: str, body: dict = JSON_BODY):
        """Replace across the pages the reviewer confirmed; each changed page is a new revision."""
        who = auth.current(request, store, write=True, role="reviewer")
        act_or_404(act_id)
        reading, latest = library.reading(act_id), store.latest_pages(act_id)
        pages = {p["page"]: p for p in reading["pages"]}
        final = {p["page"]: p for p in content.final_pages(reading, latest)}
        targets = body.get("pages") or {}
        if not isinstance(targets, dict) or not targets or len(targets) > 1000:
            raise HTTPException(422, "pages must map page numbers to the revisions shown in the preview")
        find, replacement = str(body.get("find", "")), str(body.get("replace", ""))
        case, word = bool(body.get("case")), bool(body.get("word"))
        changed, conflicts = [], []
        for key, expected in targets.items():
            number = int(key)
            if number not in pages:
                raise HTTPException(422, f"no page {number}")
            try:
                units, count = content.replace_in(final[number]["units"], find, replacement, case, word)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            if not count:
                continue
            previous = latest.get(number)
            try:
                revision = store.save_page(
                    act_id,
                    number,
                    int(expected),
                    who["email"],
                    reading_sha256=content.reading_sha256(pages[number]),
                    units=units,
                    status=previous["status"] if previous else "edited",
                    resolutions=previous["resolutions"] if previous else {},
                    comment=f"Replaced {find!r} with {replacement!r} ({count}×) across the Act"[:2000],
                )
            except Conflict:
                conflicts.append(number)
                continue
            changed.append({"page": number, "revision": revision, "replacements": count})
        return {"changed": changed, "conflicts": conflicts}

    @app.get("/api/documents/{act_id}/stream")
    def stream(request: Request, act_id: str, start: int = 1, count: int = 8):
        """Final content of a run of pages, for reading the Act continuously: `start` counts the
        document's pages from 1, whatever their printed numbers (a law cut from a Gazette issue
        begins on the issue's page 9, say)."""
        auth.current(request, store)
        act_or_404(act_id)
        reading, latest = library.reading(act_id), store.latest_pages(act_id)
        final = {p["page"]: p for p in content.final_pages(reading, latest)}
        out = []
        first = max(0, start - 1)
        for page in reading["pages"][first : first + max(1, min(count, 20))]:
            revision = latest.get(page["page"])
            resolved = set(revision["resolutions"]) if revision else set()
            out.append(
                {
                    "page": page["page"],
                    "units": final[page["page"]]["units"],
                    "revision": final[page["page"]]["revision"],
                    "status": revision["status"] if revision else None,
                    "resolutions": revision["resolutions"] if revision else {},
                    "open_high": sum(
                        1
                        for f in content.page_flags(page)
                        if f["severity"] == "high" and f["id"] not in resolved
                    ),
                    "open": sum(
                        1
                        for f in content.page_flags(page)
                        if f["severity"] != "low" and f["id"] not in resolved
                    ),
                }
            )
        return {"pages": out, "total": len(reading["pages"])}

    @app.get("/preview/{act_id}")
    def public_preview_page(act_id: str):
        """The public site's page for a document, built from web/ (data comes from the API below)."""
        return FileResponse(STATIC / "public-preview" / "preview.html")

    def document_of(work: str, language: str | None) -> str | None:
        """The workbench's document for a work: in this language if it has it, else another."""
        return next(
            (
                f"{work}-{lang}"
                for lang in (language, "en", "fr", "rw")
                if lang and library.has(f"{work}-{lang}")
            ),
            None,
        )

    def standing(act: dict) -> dict | None:
        """Whether the document is current, as its collection was prepared to tell: repealed by a later
        text (and the law in force now), as RLRC's listing files it, or not yet known; what amends
        it; a law it was made under that is since repealed; and the instruments made under it."""
        current = act.get("currency")
        if not current:
            return None
        language = act.get("language")

        def named(item: dict) -> dict:
            return {"title": item["title"], "id": document_of(item["work"], language)}

        out = {k: current[k] for k in ("status", "basis", "disagrees") if k in current}
        for key in ("repealed_by", "amended_by"):
            if current.get(key):
                out[key] = [named(item) for item in current[key]]
        if current.get("latest"):
            out["latest"] = named(current["latest"])
        if current.get("parents_repealed"):
            out["parents_repealed"] = [
                {**named(p), "repealed_by": [named(r) for r in p["repealed_by"]]}
                for p in current["parents_repealed"]
            ]
        enabled = [d for d in (document_of(w, language) for w in act.get("enables") or []) if d]
        if enabled:
            listed = sorted(
                (library.listed(d) for d in enabled), key=lambda d: (-(d.get("year") or 0), d["title"])
            )
            out["enables"] = {
                "total": len(listed),
                "shown": [{"id": d["id"], "title": d["title"]} for d in listed[:50]],
            }
        return out

    def current_details(record: dict | None, view: dict) -> dict:
        """The saved details record, or a first draft from the document when none is saved yet."""
        out = {"details": view["values"], "absent": view["absent"]}
        if record:
            return {
                **out,
                "revision": record["revision"],
                "status": record["status"],
                "comment": record["comment"],
                "author": record["author"],
                "at": record["created_at"].isoformat(),
            }
        return {**out, "revision": 0, "status": "suggested"}

    def view_schema(view: dict) -> dict:
        return {
            "standard": R.SCHEMA_VERSION,
            "groups": record_view.groups(view["family"]),
            "fields": [spec.as_dict() for spec in view["specs"]],
            "absent": R.ABSENT,
            "relations": [{"value": r, "label": record_view.label_of(r)} for r in R.RELATIONS],
            "kinds": {family: list(kinds) for family, (_, kinds) in R.FAMILIES.items()},
        }

    @app.get("/api/documents/{act_id}/languages")
    def document_languages(request: Request, act_id: str):
        """The law's language versions side by side, article by article, as reviewed so far."""
        auth.current(request, store)
        act = act_or_404(act_id)
        ids = {
            lang: other
            for lang, other in (act.get("versions") or {act.get("language", "en"): act_id}).items()
        }
        versions, instrument = {}, {}
        for language, other in ids.items():
            if not library.has(other):
                continue
            final = content.final_pages(library.reading(other), store.latest_pages(other))
            versions[language] = content.articles_of(final, language)
            document = library.act(other)
            instrument[language] = {"id": other, "title": document["title"], "pages": document.get("pages")}
        rows = content.side_by_side(versions)
        return {
            "work": act.get("work") or act_id,
            "languages": list(versions),
            "instrument": instrument,
            "issue": act.get("issue"),
            "rows": rows,
            "differences": [row["article"] for row in rows if not row["complete"]],
        }

    @app.get("/api/documents/{act_id}/details")
    def document_details(request: Request, act_id: str):
        auth.current(request, store)
        act = act_or_404(act_id)
        reading, record = library.reading(act_id), store.latest_details(details_key(act))
        view = described(act, record, reading)
        suggestions = (
            record_view.suggestions_view(details.suggest(reading, act, library.manifest_for(act_id)))
            if view["place"].code == "ng"
            else {}
        )
        links = act.get("relations") or []
        if links:  # what the collection's texts say, beside details saved before they were linked
            suggestions["relations"] = {
                "value": links,
                "evidence": "the laws this text repeals or amends, and the later ones that repeal or amend it",
            }
        # The related documents the workbench holds, each opened in this document's language if it can.
        related = {}
        for link in [*(view["values"].get("relations") or []), *links]:
            work = link.get("target_id")
            if work and work not in related and document_of(work, act.get("language")):
                related[work] = document_of(work, act.get("language"))
        return {
            "standard": view_schema(view),
            "document_type": view["kind"],
            "place": view["place"].model_dump(),
            "record": current_details(record, view),
            "suggestions": suggestions,
            "related": related,
            "missing": view["missing"],
            "history": [
                {
                    "revision": r["revision"],
                    "status": r["status"],
                    "author": r["author"],
                    "at": r["created_at"].isoformat(),
                    "comment": r["comment"],
                }
                for r in store.detail_history(details_key(act))
            ],
        }

    @app.post("/api/documents/{act_id}/details")
    def save_document_details(request: Request, act_id: str, body: dict = JSON_BODY):
        """A new revision of the details; confirming needs every detail given or accounted for,
        and a record that validates against document-record-1."""
        who = auth.current(request, store, write=True, role="reviewer")
        act = act_or_404(act_id)
        status = body.get("status")
        if status not in {"draft", "confirmed"}:
            raise HTTPException(422, "status must be draft or confirmed")
        submitted = body.get("details") or {}
        family = submitted.get("identity.family")
        kind = submitted.get("identity.type")
        if family not in R.FAMILIES or kind not in R.FAMILIES[family][1]:
            raise HTTPException(422, "Choose the document's family and a kind within it")
        place, profile = record_view.place_for(act, library.manifest_for(act_id), profiles)
        specs = record_view.fields(family, kind, profile)
        try:
            given, absent = record_view.validate(submitted, body.get("absent") or {}, specs)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if status == "confirmed":
            gaps = record_view.missing(given, absent, specs)
            if gaps:
                labels = [next(s.label for s in specs if s.key == key) for key in gaps]
                raise HTTPException(
                    422, {"message": f"Give or account for: {', '.join(labels)}", "missing": gaps}
                )
            try:
                record_view.to_record(given, absent, place, profile, record_view.slugify(act_id[3:]))
            except ValueError as error:
                raise HTTPException(422, {"message": f"The record does not validate: {error}"}) from error
        try:
            revision = store.save_details(
                details_key(act),
                int(body.get("expected_revision", -1)),
                who["email"],
                details=given,
                absent=absent,
                status=status,
                comment=str(body.get("comment", ""))[:2000],
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return {"revision": revision}

    @app.get("/api/documents/{act_id}/original.pdf")
    def original(request: Request, act_id: str):
        auth.current(request, store)
        act_or_404(act_id)
        try:
            path = library.original(act_id)
        except FileNotFoundError as error:
            raise HTTPException(404, "The original file is not in staging") from error
        return FileResponse(path, media_type="application/pdf", filename=f"{act_id}.pdf")

    @app.get("/api/documents/{act_id}/public-preview")
    def public_preview(request: Request, act_id: str):
        auth.current(request, store)
        act = act_or_404(act_id)
        now = state(act)
        reading, latest, head = now["reading"], now["latest"], now["head"]
        pages = content.public_pages(now["final"], latest)
        provenance = library.provenance(act_id)
        checked_by = {r["author"] for r in latest.values() if r["status"] == "checked"}
        named = store.names(checked_by | ({head["author"]} if head else set()))
        quoted = f"/api/documents/{quote(act_id)}"
        view = now["view"]
        document = built(act, view)
        return {
            "source": content.public_source(act, library.manifest_for(act_id)),
            # The other language versions of the same law (Rwanda prints three), for the switcher.
            "versions": [
                {"language": language, "id": other, "title": library.listed(other)["title"]}
                for language, other in (act.get("versions") or {}).items()
                if library.has(other)
            ],
            "details": current_details(now["record"], view),
            "record": document.model_dump(mode="json") if document else None,
            "making": [
                {"step": step.id, "label": step.label, "event": step.event, "signed_by": step.signed_by}
                for step in record_view.steps_for(view["profile"], view["kind"])
            ],
            "view": {
                "family": view["family"],
                "kind": view["kind"],
                "place": view["place"].model_dump(),
                "groups": record_view.groups(view["family"]),
                "fields": [
                    {k: spec[k] for k in ("key", "label", "group", "kind", "choices", "columns")}
                    for spec in (s.as_dict() for s in view["specs"])
                ],
                "values": view["values"],
                "absent": view["absent"],
            },
            "provenance": {
                "retrieved_from": provenance.get("url") or act.get("url"),
                "retrieved_at": provenance.get("retrieved_at"),
                "original_sha256": provenance.get("sha256") or reading.get("input_sha256"),
                "bytes": provenance.get("bytes"),
                "media_type": act.get("media_type", "application/pdf"),
                "pages": len(pages),
                "collection": library.manifest_for(act_id).get("description", ""),
                "retrieval_note": library.manifest_for(act_id).get("retrieval_mirror", ""),
                "rights_basis": library.manifest_for(act_id).get("rights_basis", ""),
            },
            "review": {
                "readers": content.reader_names(reading),
                "pages": len(pages),
                "checked_pages": sum(page["checked"] for page in pages),
                "reviewers": sorted({named.get(email, "a reviewer") for email in checked_by}),
                "approval": {
                    "decision": head["decision"],
                    "by": named.get(head["author"], "an approver"),
                    "at": head["created_at"].isoformat(),
                    "current": now["approved"],
                }
                if head
                else None,
            },
            "pages": pages,
            "content_sha256": now["content_sha256"],
            "details_sha256": now["details_sha256"],
            "downloads": [
                {"label": "Text", "format": "TXT", "href": f"{quoted}/text"},
                {"label": "Akoma Ntoso", "format": "XML", "href": f"{quoted}/akn"},
                {
                    "label": "Original",
                    "format": "PDF",
                    "href": f"{quoted}/original.pdf",
                    "note": "On the public site only if the rights review allows",
                },
            ],
            "status": {
                "approved": now["approved"],
                "pages": len(pages),
                "checked_pages": sum(page["checked"] for page in pages),
                "details": now["readiness"]["details"],
            },
        }

    @app.get("/api/documents/{act_id}/akn")
    def akn_export(request: Request, act_id: str):
        auth.current(request, store)
        act = act_or_404(act_id)
        now = state(act)
        view = now["view"]
        xml = akn.build(
            act,
            now["final"],
            now["content_sha256"],
            doc_record=built(act, view),
            status=(now["record"] or {}).get("status", "suggested"),
            steps={step.id: step.label for step in record_view.steps_for(view["profile"], view["kind"])},
            details_sha256=now["details_sha256"],
        )
        return Response(
            xml,
            media_type="application/akn+xml",
            headers={"Content-Disposition": f'attachment; filename="{act_id}.akn.xml"'},
        )

    @app.get("/api/documents/{act_id}/text", response_class=PlainTextResponse)
    def text(request: Request, act_id: str):
        auth.current(request, store)
        act_or_404(act_id)
        return content.plain_text(content.final_pages(library.reading(act_id), store.latest_pages(act_id)))

    @app.get("/api/audit")
    def audit(request: Request, act: str | None = None):
        auth.current(request, store)
        return [{**e, "at": e["at"].isoformat()} for e in store.events(act_id=act, limit=300)]

    privileged.install(app, store, relying)
    access_section.install(app, store, access, relying)
    operators_section.install(app, store, relying)

    @app.exception_handler(HTTPException)
    async def problems(request: Request, error: HTTPException):
        detail = error.detail if isinstance(error.detail, dict) else {"message": str(error.detail)}
        if request.url.path.startswith(("/api", "/auth/dev", "/auth/logout")):
            return JSONResponse(detail, status_code=error.status_code)
        return HTMLResponse(
            f"<!doctype html><meta charset='utf-8'><p>{html.escape(detail.get('message', 'Error'))}</p><p><a href='/'>Back</a></p>",
            status_code=error.status_code,
        )

    return app


def from_environment() -> FastAPI:
    settings.load()
    config = auth.OIDCConfig.from_environment()
    return create_app(
        Store(engine_from_environment()),
        content.Library(),
        auth.OIDC(config) if config else None,
        dev_login=auth.dev_login_enabled(),
        access=access_section.from_environment(),
        relying=privileged.relying_from_environment(auth.dev_login_enabled()),
    )


def __getattr__(name: str):  # `uvicorn workbench.app:app` builds the app on first use
    if name == "app":
        return from_environment()
    raise AttributeError(name)
