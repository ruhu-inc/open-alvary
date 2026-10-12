"""The review workbench web application (operator only).

    ALVARY_REVIEW_DATABASE_URL=... uvicorn workbench.app:app --host 127.0.0.1 --port 8090

Put it on the web only behind TLS (a reverse proxy) with ALVARY_REVIEW_BASE_URL set to its public
https address and an OpenID Connect client configured; see the runbook.
"""

import html
import os
from pathlib import Path
from urllib.parse import quote

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
from workbench import akn, auth, content, details, record_view
from workbench.store import Conflict, Store, engine_from_environment

STATIC = Path(__file__).with_name("static")
DECISIONS = {"keep", "use_check", "edited", "dismissed"}
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
    store: Store, library: content.Library, oidc: auth.OIDC | None = None, dev_login: bool = False
) -> FastAPI:
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
    def login():
        if not oidc:
            raise HTTPException(404, "Sign-in with an identity provider is not configured")
        return RedirectResponse(oidc.authorize_url(store), status_code=303)

    @app.get("/auth/callback")
    def callback(request: Request, state: str = "", code: str = ""):
        if not oidc or not state or not code:
            raise HTTPException(400, "Invalid sign-in response")
        email = oidc.finish(store, state, code)
        token, _ = store.start_session(email)
        # A same-site navigation home, so the strict session cookie is sent with it.
        response = HTMLResponse(
            "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0;url=/'>"
            "<p>Signed in. <a href='/'>Continue</a></p>"
        )
        response.set_cookie(auth.COOKIE, token, **auth.cookie_options(request))
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

    profiles = jurisdiction_registry()

    def details_key(document: dict) -> str:
        """Whose details these are: the work, shared by its language versions (Rwanda's three
        texts of one law), or the document itself."""
        return document.get("work") or document["id"]

    def described(act: dict, record: dict | None, reading: dict) -> dict:
        """The document's details in the record view: values, what is accounted for, its fields."""
        place, profile = record_view.place_for(act, library.manifest_for(act["id"]), profiles)
        if record:
            values, absent = record["details"], record["absent"]
            if not record_view.is_view(values):  # saved under the Nigerian DETAILS-1: read it in this view
                values, absent = record_view.from_legacy(values, absent)
        elif place.code == "ng":
            values, absent = record_view.from_legacy(
                *details.initial(details.suggest(reading, act, library.manifest_for(act["id"])))
            )
        else:
            values = record_view.base_values(act, library.manifest_for(act["id"]))
            absent = dict(act.get("suggested_absent") or {})
        if act.get("language") and isinstance(values.get("expressions"), list):
            # One record for the work; each language version is "this text" on its own page.
            values = {
                **values,
                "expressions": [
                    {**row, "this_text": "yes" if row.get("language") == act["language"] else "no"}
                    for row in values["expressions"]
                ],
            }
        family, kind = values["identity.family"], values["identity.type"]
        specs = record_view.fields(family, kind, profile)
        return {
            "values": values,
            "absent": absent,
            "specs": specs,
            "place": place,
            "profile": profile,
            "family": family,
            "kind": kind,
            "missing": record_view.missing(values, absent, specs),
        }

    def built(act: dict, view: dict):
        """The validated record (document-record-1), or None while details are incomplete."""
        try:
            return record_view.to_record(
                view["values"],
                view["absent"],
                view["place"],
                view["profile"],
                record_view.slugify(act["id"][3:]),
            )
        except (ValueError, KeyError):
            return None

    def state(document: dict) -> dict:
        """Everything that decides a document's review status, read once: its pages, its details,
        and whether the latest approval still covers both exactly."""
        reading, latest = library.reading(document["id"]), store.latest_pages(document["id"])
        final = content.final_pages(reading, latest)
        record = store.latest_details(details_key(document))
        view = described(document, record, reading)
        sha, details_sha = content.content_sha256(final), content.details_sha256(record)
        approvals = store.approvals(document["id"])
        head = approvals[0] if approvals else None
        approved = bool(
            head
            and head["decision"] == "approved"
            and head["content_sha256"] == sha
            and head.get("details_sha256") == details_sha
            and record
            and record["status"] == "confirmed"
        )
        return {
            "reading": reading,
            "latest": latest,
            "final": final,
            "record": record,
            "content_sha256": sha,
            "details_sha256": details_sha,
            "approvals": approvals,
            "head": head,
            "approved": approved,
            "view": view,
            "readiness": content.readiness(reading, latest, content.details_state(record, view["missing"])),
        }

    def document_summary(document: dict) -> dict:
        now = state(document)
        reading, latest, ready, head = now["reading"], now["latest"], now["readiness"], now["head"]
        last = store.events(act_id=document["id"], limit=1)
        approval = None
        if head and head["decision"] == "approved":
            approval = "approved" if now["approved"] else "out_of_date"
        return {
            "id": document["id"],
            "title": document["title"],
            "type": document.get("type", "act"),
            "year": document.get("year"),
            "jurisdiction": document.get("jurisdiction"),
            "pages": ready["pages"],
            "checked": ready["pages"] - len(ready["unchecked_pages"]),
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

    @app.get("/api/overview")
    def overview(request: Request):
        """Everything the home page shows: totals, each document's state, where to continue."""
        who = auth.current(request, store)
        rows = [document_summary(d) for d in library.documents()]
        resume = None
        last = store.last_event(who["email"], "page_saved")
        if last:
            row = next((r for r in rows if r["id"] == last["act_id"]), None)
            if row:
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
        documents = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        titles = {r["id"]: r["title"] for r in rows}
        return {
            "totals": {
                "documents": len(documents),
                "pages": sum(r["pages"] for r in documents),
                "checked": sum(r["checked"] for r in documents),
                "open_high": sum(r["open_high"] for r in documents),
                "approved": sum(1 for r in documents if r["approval"] == "approved"),
                "ready": sum(1 for r in documents if r["ready"] and r["approval"] != "approved"),
            },
            "documents": documents,
            "types": sorted({r["type"] for r in documents}),
            "resume": resume,
            "activity": [
                {
                    "at": e["at"].isoformat(),
                    "actor": e["actor"],
                    "action": e["action"],
                    "id": e["act_id"],
                    "title": titles.get(e["act_id"]),
                    "page": e["page"],
                }
                for e in store.events(limit=200)
                if e["action"] in {"page_saved", "details_saved", "act_approved", "act_revoked"}
            ][:12],
        }

    @app.get("/api/documents")
    def acts(request: Request):
        auth.current(request, store)
        out = []
        for act in library.documents():
            now = state(act)
            reading, ready, head = now["reading"], now["readiness"], now["head"]
            out.append(
                {
                    "id": act["id"],
                    "title": act["title"],
                    "pages": ready["pages"],
                    "checked": ready["pages"] - len(ready["unchecked_pages"]),
                    "open_high_flags": len(ready["open_high_flags"]),
                    "flags": reading["summary"]["flags"],
                    "approval": head["decision"] if head else None,
                    "approval_current": now["approved"],
                }
            )
        return out

    @app.get("/api/documents/{act_id}")
    def act_detail(request: Request, act_id: str):
        auth.current(request, store)
        act = act_or_404(act_id)
        now = state(act)
        reading, latest = now["reading"], now["latest"]
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
                }
            )
        return {
            "act": {**act, "rights_basis": library.manifest_for(act_id).get("rights_basis", "")},
            "readers": {"primary": reading["primary"], "checks": reading["checks"]},
            "pages": pages,
            "readiness": now["readiness"],
            "content_sha256": now["content_sha256"],
            "details_sha256": now["details_sha256"],
            "approved": now["approved"],
            "approvals": [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in a.items()}
                for a in now["approvals"]
            ],
            "materials": content.MATERIALS,
        }

    @app.get("/api/documents/{act_id}/pages/{page}")
    def page_detail(request: Request, act_id: str, page: int):
        auth.current(request, store)
        act_or_404(act_id)
        _, found = reading_page(act_id, page)
        history = store.page_history(act_id, page)
        latest = history[0] if history else None
        return {
            "page": page,
            "machine_units": content.units_from(found),
            "machine_html": content.machine_html(found),
            "flags": content.page_flags(found),
            "checks": found["checks"],
            "regions": content.regions(library.staging_for(act_id), act_id, page),
            "lines": content.lines(library.staging_for(act_id), act_id, page),
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
        now = state(act_or_404(act_id))
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
                rights_basis=basis,
                materials={k: bool(v) for k, v in materials.items() if k in content.MATERIALS},
                note=note,
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
            "approval": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in head.items()}
            if head
            else None,
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
        """Final content of a run of pages, for reading the Act continuously."""
        auth.current(request, store)
        act_or_404(act_id)
        reading, latest = library.reading(act_id), store.latest_pages(act_id)
        final = {p["page"]: p for p in content.final_pages(reading, latest)}
        out = []
        for page in reading["pages"]:
            if not start <= page["page"] < start + max(1, min(count, 20)):
                continue
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
        present = {d["id"] for d in library.documents()}
        ids = {
            lang: other
            for lang, other in (act.get("versions") or {act.get("language", "en"): act_id}).items()
        }
        versions, instrument = {}, {}
        for language, other in ids.items():
            if other not in present:
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
        return {
            "standard": view_schema(view),
            "document_type": view["kind"],
            "place": view["place"].model_dump(),
            "record": current_details(record, view),
            "suggestions": suggestions,
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
                {"language": language, "id": other, "title": library.act(other)["title"]}
                for language, other in (act.get("versions") or {}).items()
                if any(d["id"] == other for d in library.documents())
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
    )


def __getattr__(name: str):  # `uvicorn workbench.app:app` builds the app on first use
    if name == "app":
        return from_environment()
    raise AttributeError(name)
