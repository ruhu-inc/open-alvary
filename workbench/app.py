"""The review workbench web application (operator only).

    ALVARY_REVIEW_DATABASE_URL=... uvicorn workbench.app:app --host 127.0.0.1 --port 8090

Put it on the web only behind TLS (a reverse proxy) with ALVARY_REVIEW_BASE_URL set to its public
https address and an OpenID Connect client configured; see the runbook.
"""

import hmac
import html
import os
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ingestion import cited_names, render, settings
from schema import record as R
from schema.jurisdictions import registry as jurisdiction_registry
from workbench import access as access_section
from workbench import (
    akn,
    auth,
    citations,
    claims,
    content,
    details,
    editorial,
    privileged,
    publication,
    queue,
    record_view,
    serialize,
)
from workbench import audit as audit_mod
from workbench import operators as operators_section
from workbench import publishing as publishing_section
from workbench import traffic as traffic_section
from workbench.review import Review, blind, status_research_pending, to_do
from workbench.store import (
    Conflict,
    Store,
    _aware,
    approval,
    blocked_label,
    detail_revision,
    engine_from_environment,
    page_revision,
    review_audit,
    work_claim,
    work_release,
)
from workbench.store import now as store_now
from workbench.store import said as error_said

STATIC = Path(__file__).with_name("static")
DECISIONS = {"keep", "use_check", "edited", "dismissed"}
SENTENCE_END = re.compile(r"[.;:!?—–]['’”\")\]]*$")  # a page ending here finishes its paragraph
JSON_BODY = Body(...)
OPTIONAL_JSON_BODY = Body(default=None)
# In place of a page image for a document published as a web page (a National Industrial Court judgment),
# which has none: the reviewer compares the text with the official page itself.
WEB_PAGE_IMAGE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1240" height="300" viewBox="0 0 1240 300">'
    b'<rect width="1240" height="300" fill="#f4f4f4"/>'
    b'<text x="620" y="130" font-family="sans-serif" font-size="34" text-anchor="middle" fill="#333">'
    b"No page image: this document was published as a web page.</text>"
    b'<text x="620" y="190" font-family="sans-serif" font-size="28" text-anchor="middle" fill="#555">'
    b"Compare the text with the official source: its link is above.</text></svg>"
)
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
        account = getattr(request.state, "account", None)
        if (
            account
            and response.status_code < 400
            and request.url.path.startswith("/api/documents/")
            and request.headers.get("X-Claim-Token")
            and claims.claims_mode() != "off"
        ):
            parts = request.url.path.split("/")
            if len(parts) >= 4 and parts[3]:
                # Off the event loop: this talks to the database, and the loop serves every other request
                # of this copy. And never at the cost of the answer: the route's work is done and saved.
                try:
                    await run_in_threadpool(
                        _touch_claim_on_activity,
                        unquote(parts[3]),
                        account,
                        request.headers["X-Claim-Token"],
                    )
                except Exception as error:  # noqa: BLE001 - a renewal that failed is tried at the next request
                    print(
                        f"workbench: claim not renewed on activity: {error_said(error)}",
                        file=sys.stderr,
                        flush=True,
                    )
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
        operator = store.operator(email)
        # A same-site navigation home, so the strict session cookie is sent with it.
        response = HTMLResponse(
            "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0;url=/'>"
            "<p>Signed in. <a href='/'>Continue</a></p>"
        )
        response.set_cookie(
            auth.COOKIE, token, **auth.cookie_options(request, max_age=auth.cookie_max_age(operator))
        )
        response.delete_cookie(auth.FLOW_COOKIE, path="/auth")
        return response

    @app.post("/auth/dev")
    def dev(request: Request, body: dict = JSON_BODY):
        if not dev_login or not auth.is_loopback(request):
            raise HTTPException(404, "Not available")
        email = str(body.get("email", "")).strip().lower()
        operator = store.operator(email)
        if not operator:
            raise HTTPException(403, "Not an operator")
        token, _ = store.start_session(email)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE, token, **auth.cookie_options(request, max_age=auth.cookie_max_age(operator))
        )
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
            "traffic": access is not None and traffic_section.ROLE in person["roles"],
            "operators": relying is not None and operators_section.ROLE in person["roles"],
            "publishing": bool(person["roles"] & publishing_section.READERS),
            # Many agents at once (docs: multi-agent-review-design): whether works are claimed, whether
            # this account is an agent's, and whether this person is shown the Agents screen, which has
            # nothing to show until claims are on or an agent account exists.
            "claims": claims.claims_mode(),
            "agent": bool(person.get("agent")),
            "agents": "admin" in person["roles"]
            and (claims.claims_mode() != "off" or store.has_agent_accounts()),
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
        """How the text before this page ends, as now reviewed: whether this page's first paragraph may
        continue it (TXP-02 rule 8). The paragraph is found as the published text finds it
        (serialize.serialize): a page with no words is passed over (a blank page, or one that holds only
        a seal once its letters are removed), and a table ends the paragraph before it, so nothing
        after one can be joined."""
        numbers = [p["page"] for p in reading["pages"]]
        if page not in numbers:
            return None
        latest = store.latest_pages(act_id)
        for before in reversed(reading["pages"][: numbers.index(page)]):
            revision = latest.get(before["page"])
            for unit in reversed(revision["units"] if revision else content.units_from(before)):
                if unit.get("kind") == "table":
                    return None
                if words := serialize.unit_words(unit):
                    return {
                        "page": before["page"],
                        "text": words[-160:],
                        "hyphen": words.endswith("-"),
                        "ends_sentence": bool(SENTENCE_END.search(words)),
                    }
        return None

    profiles = jurisdiction_registry()
    review = Review(store, library, profiles)
    details_key, described, built, state = review.details_key, review.described, review.built, review.state
    shown_title = review.shown_title

    def work_documents(work_id: str) -> list[dict]:
        return [d for d in library.documents() if details_key(d) == work_id]

    def sibling_act_ids(act: dict) -> set[str]:
        work_id = details_key(act)
        ids = {act["id"]} | {d["id"] for d in work_documents(work_id)}
        for other in (act.get("versions") or {}).values():
            if other:
                ids.add(str(other))
        return ids

    def current_claim(work_id: str) -> dict | None:
        if claims.claims_mode() == "off":
            return None
        with store.engine.connect() as db:
            cutoff = sql_text("CURRENT_TIMESTAMP") if db.dialect.name == "postgresql" else store_now()
            row = db.execute(
                select(work_claim).where(work_claim.c.work_id == work_id, work_claim.c.expires_at > cutoff)
            ).first()
        if row is None:
            return None
        return {
            "work_id": row.work_id,
            "account": row.account,
            "label": row.label,
            "task": row.task,
            "claimed_at": _aware(row.claimed_at).isoformat(),
            "renewed_at": _aware(row.renewed_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
        }

    def _touch_claim_on_activity(act_id: str, account: str, claim_token: str) -> None:
        """Renew an active claim on signed-in /api/documents/{act_id}/... activity (at most once/min)."""
        if not claim_token or not library.has(act_id):
            return
        claims.touch(store.engine, details_key(library.listed(act_id)), account, claim_token)

    work_cache_lock = threading.Lock()
    work_cache: dict = {"at": 0.0, "snapshot": None, "evicted": set()}

    def _invalidate_work_cache() -> None:
        with work_cache_lock:
            work_cache["at"] = 0.0
            work_cache["snapshot"] = None
            work_cache["evicted"] = set()

    def _evict_work(work_id: str) -> None:
        with work_cache_lock:
            work_cache["evicted"].add(work_id)

    work_index_cache: dict = {"key": None, "index": None}

    def _work_index() -> tuple[dict, dict]:
        """The collection's works, each with its documents, size and place in the queue: made once for
        a list of documents, from their manifest entries alone. No reading is opened here. Opening
        each document's reading to count its pages, on every call, took 0.4 s for 160 documents and
        would take minutes for a collection of tens of thousands (load test, 2026-11-19)."""
        documents = library.documents()
        key = (id(documents), len(documents))
        with work_cache_lock:
            if work_index_cache["key"] == key:
                return work_index_cache["index"]
        grouped: dict[str, list[dict]] = {}
        doc_to_work: dict[str, str] = {}
        for doc in documents:
            wid = details_key(doc)
            grouped.setdefault(wid, []).append(doc)
            doc_to_work[doc["id"]] = wid
        works_by_id: dict[str, dict] = {}
        for wid, docs in grouped.items():
            docs.sort(key=lambda d: d["id"])
            known = [d.get("page_count") for d in docs]
            works_by_id[wid] = {
                "work_id": wid,
                "docs": docs,
                # None where a manifest entry gives no count: counted once, when a limit asks (_work_pages).
                "total_pages": sum(int(n) for n in known) if all(n is not None for n in known) else None,
                "jurisdictions": {str(d.get("jurisdiction") or "").lower() for d in docs},
                "kinds": {str(d.get("type") or d.get("document_type") or "").lower() for d in docs},
                "importance": min(queue.importance(d) for d in docs),
            }
        with work_cache_lock:
            work_index_cache["key"], work_index_cache["index"] = key, (works_by_id, doc_to_work)
        return works_by_id, doc_to_work

    def _work_pages(work: dict) -> int:
        """A work's pages in all its documents; read from the readings once where the manifest is silent."""
        if work["total_pages"] is None:
            total = 0
            for d in work["docs"]:
                count = d.get("page_count")
                if count is None:
                    try:
                        count = len(library.reading(d["id"])["pages"])
                    except Exception:  # noqa: BLE001 - a reading that cannot be opened has no pages to count
                        count = 0
                total += int(count or 0)
            work["total_pages"] = total
        return work["total_pages"]

    def _cached_work_snapshot() -> tuple[dict, set[str]]:
        """10-second cached work index, stamps, live claims, approved/parked sets for claim-next (§4.6)."""
        now_mono = time.monotonic()
        with work_cache_lock:
            if work_cache["snapshot"] is not None and now_mono - work_cache["at"] < 10.0:
                return work_cache["snapshot"], set(work_cache["evicted"])

        works_by_id, doc_to_work = _work_index()

        activity = store.activity_stamps()
        details_stamps = store.detail_stamps()
        live_claims = {c["work_id"] for c in claims.list_claims(store, include_expired=False)}

        with store.engine.connect() as db:
            latest_app = (
                select(approval.c.act_id, func.max(approval.c.revision).label("rev"))
                .group_by(approval.c.act_id)
                .subquery()
            )
            approved_acts = {
                row.act_id
                for row in db.execute(
                    select(approval.c.act_id)
                    .join(
                        latest_app,
                        (approval.c.act_id == latest_app.c.act_id)
                        & (approval.c.revision == latest_app.c.rev),
                    )
                    .where(approval.c.decision == "approved")
                )
            }
            approved_works = {doc_to_work[a] for a in approved_acts if a in doc_to_work}

            latest_rel = (
                select(work_release.c.work_id, func.max(work_release.c.id).label("id"))
                .group_by(work_release.c.work_id)
                .subquery()
            )
            parked_works = {
                row.work_id
                for row in db.execute(
                    select(work_release.c.work_id)
                    .join(
                        latest_rel,
                        (work_release.c.work_id == latest_rel.c.work_id)
                        & (work_release.c.id == latest_rel.c.id),
                    )
                    .where(work_release.c.outcome == "parked")
                )
            }

        snapshot = {
            "works_by_id": works_by_id,
            "doc_to_work": doc_to_work,
            "activity": activity,
            "details_stamps": details_stamps,
            "live_claims": live_claims,
            "approved_works": approved_works,
            "parked_works": parked_works,
        }
        with work_cache_lock:
            work_cache["at"] = time.monotonic()
            work_cache["snapshot"] = snapshot
            work_cache["evicted"] = set()
            return snapshot, set()

    def _work_standing(work_id: str) -> tuple[bool, bool]:
        """Whether any document of the work is approved, and whether the work was last left for the owner:
        as the database has it now, not as the ten-second list "Claim next" works from had it."""
        with store.engine.connect() as db:
            approved = any(
                db.execute(
                    select(approval.c.decision)
                    .where(approval.c.act_id == doc["id"])
                    .order_by(approval.c.revision.desc())
                    .limit(1)
                ).scalar()
                == "approved"
                for doc in work_documents(work_id)
            )
            last = db.execute(
                select(work_release.c.outcome)
                .where(work_release.c.work_id == work_id)
                .order_by(work_release.c.id.desc())
                .limit(1)
            ).scalar()
        return approved, last == "parked"

    def _claims_open(task: str = "review") -> None:
        """Works are claimed only while the switch is on, and audited only while claims are enforced: with
        claims off or only logged, a save is not held to its claim, and an auditor's would go into the live
        page instead of the audit's record."""
        mode = claims.claims_mode()
        if mode == "off":
            raise claims.ClaimError(409, claims.MSG_CLAIMS_OFF, "claims_off")
        if task == "audit" and mode != "enforce":
            raise claims.ClaimError(409, claims.MSG_AUDIT_NEEDS_ENFORCE, "audit_needs_enforce")

    def _claim_response(claimed: dict, *, pages_drawn_by_doc: dict[str, list[int]] | None = None) -> dict:
        wid = claimed["work_id"]
        out_docs = []
        for doc in work_documents(wid):
            pages_count = doc.get("page_count")
            if pages_count is None:
                try:
                    pages_count = len(library.reading(doc["id"])["pages"])
                except Exception:
                    pages_count = 0
            entry = {
                "id": doc["id"],
                "pages": int(pages_count or 0),
                "address": f"#/doc/{doc['id']}",
            }
            if pages_drawn_by_doc is not None:
                entry["pages_drawn"] = pages_drawn_by_doc.get(doc["id"], [])
            out_docs.append(entry)
        result = {
            "work_id": wid,
            "task": claimed["task"],
            "token": claimed["token"],
            "expires_at": claimed["expires_at"],
            "documents": out_docs,
        }
        if "rotated" in claimed:
            result["rotated"] = claimed["rotated"]
        return result

    def _serialize_audit_row(row: dict) -> dict:
        return {k: (_aware(v).isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}

    def document_summary(document: dict) -> dict:
        now = state(library.act(document["id"]))
        reading, latest, ready, head = now["reading"], now["latest"], now["readiness"], now["head"]
        last = store.events(act_id=document["id"], limit=1)
        approval = None
        if head and head["decision"] == "approved":
            approval = "approved" if now["approved"] else "out_of_date"
        held = bool(document.get("text_held"))
        return with_status(
            {
                **listed(document),
                "title": shown_title(document),
                "pages": 0 if held else ready["pages"],
                "checked": 0
                if held
                else ready["pages"] - len({*ready["unchecked_pages"], *ready["unresolved_flag_pages"]}),
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
            "title": review.prepared_title(document),
            "type": document.get("type", "act"),
            "year": document.get("year"),
            "jurisdiction": document.get("jurisdiction"),
            "language": document.get("language"),
            **({"text_held": True} if document.get("text_held") else {}),
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
        held = bool(document.get("text_held"))
        return with_status(
            {
                **listed(document),
                "pages": 0 if held else document["page_count"],
                "checked": 0,
                "attention": 0,
                "open_high": 0 if held else flags.get("high", 0),
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

    # Each worked-on document's page tasks, kept until its latest event or details revision changes.
    task_cache: dict[str, tuple[tuple, list[dict]]] = {}

    @app.get("/api/queue")
    def work_queue(request: Request, kind: str = "", offset: int = 0, limit: int = 100):
        """The work waiting across documents (workbench.queue): pages to review, flags to resolve and
        proposals to review, each with its document, kind, priority and age; optionally of one kind."""
        auth.current(request, store)
        activity, saved, began = store.activity_stamps(), store.detail_stamps(), store.activity_began()
        tasks = []
        for document in library.documents():
            stamp = (activity.get(document["id"]), saved.get(details_key(document)))
            if stamp == (None, None) and document.get("page_count") is not None:
                tasks.append(queue.not_started({**document, **listed(document)}))
                continue
            cached = task_cache.get(document["id"])
            if cached is None or cached[0] != stamp or stamp == (None, None):
                now = state(library.act(document["id"]))
                found = queue.page_tasks(
                    listed(document), now["readiness"], now["latest"], began.get(document["id"])
                )
                cached = task_cache[document["id"]] = (stamp, found)
            tasks += [{**t, "started": stamp != (None, None)} for t in cached[1]]

        def document_of(act_id: str) -> dict:
            return listed(library.listed(act_id)) if library.has(act_id) else {"id": act_id, "title": act_id}

        tasks += queue.proposal_tasks(
            store.legal_event_revisions(),
            store.instruction_revisions(),
            document_of,
            store.citation_revisions(),
        )
        counts = {
            name: sum(t["kind"] == name for t in tasks)
            for name in ("review", "details", "flags", "events", "instructions", "citations")
        }
        chosen = queue.ordered([t for t in tasks if not kind or t["kind"] == kind])
        limit, offset, moment = max(1, min(limit, 500)), max(0, offset), datetime.now(UTC)
        live_claims = {c["work_id"]: c for c in claims.list_claims(store, include_expired=False)}

        def _with_claim(item: dict) -> dict:
            wid = details_key(library.listed(item["id"])) if library.has(item["id"]) else item["id"]
            return {**queue.shown(item, moment), "claim": live_claims.get(wid)}

        return {
            "total": len(chosen),
            "offset": offset,
            "limit": limit,
            "counts": counts,
            "tasks": [_with_claim(t) for t in chosen[offset : offset + limit]],
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
        shown = rows[offset : offset + limit]
        # Where each shown document stands in public view (the latest run, and whether it awaits the next).
        statuses, decided = store.publication_statuses(), {}
        for row in shown:
            code = row.get("jurisdiction") or ""
            if code not in decided:
                decided[code] = publishing_section.decisions(code)
        shown = [
            {
                **row,
                "publication": publishing_section.short(
                    row.get("approval"), statuses.get(row["id"]), decided[row.get("jurisdiction") or ""]
                ),
            }
            for row in shown
        ]
        return {
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "documents": shown,
        }

    def withheld_wording(act: dict, now: dict) -> dict | None:
        """For a record with names withheld, what the catalogue will show of it (title, citation, summary,
        other names), whether a withheld name is still in it, and the hash an approval records; else None."""
        record = built(act, now["view"])
        if record is None:
            return None
        from operations import publish

        if not publish.withheld_names(record):
            return None
        work = act.get("work") or act["id"]
        return publish.published_wording(work, record) | {"hash": publish.wording_hash(work, record)}

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
        wording = withheld_wording(act, now)
        return {
            "act": {
                **act,
                "title": shown_title(act),
                "rights_basis": library.manifest_for(act_id).get("rights_basis", ""),
            },
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
            "materials": content.materials_for(act, withheld=wording is not None),
            "withheld_wording": wording,
            "claim": current_claim(details_key(act)),
            "publication": publishing_section.standing(
                act,
                now,
                store.publication_statuses().get(act_id),
                publishing_section.decisions(act.get("jurisdiction") or ""),
            ),
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
                "resolutions": content.standing(found, latest["resolutions"]),
                "comment": latest["comment"],
                "author": latest["author"],
                "label": latest.get("label"),  # which of an account's agents saved it
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
        if library.web_page(act_id):
            return Response(
                WEB_PAGE_IMAGE, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"}
            )
        try:
            image = library.image(act_id, page)
        except FileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        except (render.Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise HTTPException(502, f"Could not render page {page} of {act_id}: {error}") from error
        return FileResponse(
            image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.post("/api/documents/{act_id}/pages/{page}")
    def save_page(request: Request, act_id: str, page: int, body: dict = JSON_BODY):
        who = auth.current(request, store, write=True, role="reviewer")
        act = act_or_404(act_id)
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
        if not isinstance(resolutions, dict):
            raise HTTPException(422, "resolutions must refer to this page's flags")
        # A draft kept in a browser, or a page loaded before the page was read again, still carries the
        # last revision's decisions for flags the new reading no longer has: those go with the flags.
        # Any other decision about a flag the page does not have is refused as before.
        earlier = store.page_history(act_id, page)[:1]
        gone = set(earlier[0]["resolutions"]) - known if earlier else set()
        resolutions = {flag: value for flag, value in resolutions.items() if flag not in gone}
        if not set(resolutions) <= known:
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
        token = request.headers.get("X-Claim-Token")
        with store.engine.begin() as db:
            claim_info = claims.guard(
                db,
                act,
                who,
                token=token,
                write=True,
                act_id=act_id,
                approved_act_ids=[act_id],
            )
            if claim_info and claim_info.get("task") == "audit":
                open_audit = db.execute(
                    select(review_audit)
                    .where(
                        review_audit.c.work_id == details_key(act),
                        review_audit.c.act_id == act_id,
                        review_audit.c.page == page,
                        review_audit.c.audited_at.is_(None),
                    )
                    .order_by(review_audit.c.id.asc())
                    .limit(1)
                ).first()
                if open_audit is None:
                    raise HTTPException(404, "No open audit draw for this page.")
                rev_row = db.execute(
                    select(page_revision.c.units).where(
                        page_revision.c.act_id == act_id,
                        page_revision.c.page == page,
                        page_revision.c.revision == open_audit.reviewer_revision,
                    )
                ).first()
                reviewer_units = (
                    list(rev_row.units) if rev_row and rev_row.units else content.units_from(found)
                )
                diff = audit_mod.audit_diff(reviewer_units, units)
                try:
                    store.save_audit_submission(
                        open_audit.id,
                        auditor=who["email"],
                        auditor_label=claim_info["label"],
                        units=units,
                        resolutions=clean,
                        status=status,
                        paragraphs_changed=diff["paragraphs_changed"],
                        words_changed=diff["words_changed"],
                        other_changes=diff["other_changes"],
                        verdict=diff["verdict"],
                        db=db,
                    )
                except ValueError as error:
                    raise HTTPException(403, str(error)) from error
                except Conflict as error:
                    raise HTTPException(409, str(error)) from error
                return {
                    "audit_id": open_audit.id,
                    "audited": True,
                    "verdict": diff["verdict"],
                    "diff": diff,
                }
            label = claim_info.get("label") if claim_info else None
            task = claim_info.get("task") if claim_info else None
            try:
                revision = store.save_page(
                    act_id,
                    page,
                    int(body.get("expected_revision", -1)),
                    who["email"],
                    label=label,
                    task=task,
                    db=db,
                    reading_sha256=content.reading_sha256(found),
                    units=units,
                    status=status,
                    resolutions=clean,
                    comment=comment,
                )
            except Conflict as error:
                raise HTTPException(409, str(error)) from error
        if claim_info and claim_info.get("implicit") and claim_info.get("claim_token"):
            wid = details_key(act)
            _evict_work(wid)
            return JSONResponse(
                {
                    "revision": revision,
                    "claim_token": claim_info["claim_token"],
                    "claim_label": claim_info["label"],
                    "claim_expires_at": claim_info["claim_expires_at"],
                    "claim_work_id": wid,
                    "claim_documents": sorted(sibling_act_ids(act)),
                },
                headers={"X-Claim-Token": claim_info["claim_token"]},
            )
        return {"revision": revision}

    @app.post("/api/documents/{act_id}/approval")
    def approve(request: Request, act_id: str, body: dict = JSON_BODY):
        who = auth.current(request, store, write=True, role="approver")
        act = act_or_404(act_id)
        now = state(act, text=True)
        decision = body.get("decision")
        if decision not in {"approved", "revoked"}:
            raise HTTPException(422, "decision must be approved or revoked")
        materials = body.get("materials") or {}
        basis = str(body.get("rights_basis", "")).strip()[:2000]
        wording = withheld_wording(act, now)
        required = content.materials_for(act, withheld=wording is not None)
        if decision == "approved":
            ready = now["readiness"]
            if not ready["ready"]:
                only_status_pending = (
                    ready.get("status_pending")
                    and not ready.get("unchecked_pages")
                    and not ready.get("open_high_flags")
                    and not ready.get("stale_pages")
                    and not ready.get("unresolved_flag_pages")
                    and not ready.get("missing_checks")
                    and (ready.get("details") or {}).get("confirmed")
                    and not (ready.get("text") or {}).get("errors")
                )
                message = ready["status_pending_message"] if only_status_pending else "Not ready for approval"
                # Blind, as everywhere else: an approver may review pages too (one person may do both),
                # so the refusal names what is left to do, never which pages are the audit sample.
                raise HTTPException(409, {"message": message, "readiness": blind(ready)})
            if wording is not None and wording["names_left"]:
                raise HTTPException(
                    409,
                    "Names are withheld, but the title, citation, summary or another name would still give "
                    "one: edit the details first.",
                )
            if set(materials) != set(required) or not all(materials.values()):
                raise HTTPException(422, "Confirm every verification check")
            if not basis:
                raise HTTPException(422, "State the rights basis")
            # An approval covers what the approver was shown, not whatever is stored when the button is
            # pressed: the page sends the hashes it displayed, and a change since then is refused. A save
            # landing after this comparison leaves the approval out of date, as any later save does.
            shown = body.get("shown") if isinstance(body.get("shown"), dict) else {}
            current = {
                "content_sha256": now["content_sha256"],
                "details_sha256": now["details_sha256"],
                "text_sha256": now["text"].text_sha256,
            }
            if any(shown.get(key) != value for key, value in current.items()):
                raise HTTPException(
                    409,
                    {
                        "message": "This document changed after you opened it. Look at it again before "
                        "approving: reload the page to see the current text and details.",
                        "changed": sorted(key for key, value in current.items() if shown.get(key) != value),
                    },
                )
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
                # For a record with names withheld, what was confirmed is the wording itself (its hash):
                # publishing refuses the record if what it would show is not exactly that.
                materials={k: bool(v) for k, v in materials.items() if k in required}
                | (
                    {"withheld_wording": wording["hash"]}
                    if wording is not None and decision == "approved"
                    else {}
                ),
                note=note,
                routing=now["readiness"].get("routing") if decision == "approved" else None,
            )
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        _invalidate_work_cache()
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
        act = act_or_404(act_id)
        reading, latest = library.reading(act_id), store.latest_pages(act_id)
        pages = {p["page"]: p for p in reading["pages"]}
        final = {p["page"]: p for p in content.final_pages(reading, latest)}
        targets = body.get("pages") or {}
        if not isinstance(targets, dict) or not targets or len(targets) > 1000:
            raise HTTPException(422, "pages must map page numbers to the revisions shown in the preview")
        find, replacement = str(body.get("find", "")), str(body.get("replace", ""))
        case, word = bool(body.get("case")), bool(body.get("word"))
        planned = []
        for key, expected in targets.items():
            number = int(key)
            if number not in pages:
                raise HTTPException(422, f"no page {number}")
            try:
                units, count = content.replace_in(final[number]["units"], find, replacement, case, word)
                # The same checks as a page save: no unit past its size, nothing the corpus cannot hold.
                units = content.validate_units(units) if count else units
            except ValueError as error:
                raise HTTPException(422, f"page {number}: {error}") from error
            if count:
                planned.append((number, int(expected), units, count))
        changed, conflicts = [], []
        token = request.headers.get("X-Claim-Token")
        with store.engine.begin() as db:
            claim_info = claims.guard(
                db,
                act,
                who,
                token=token,
                write=True,
                act_id=act_id,
                approved_act_ids=[act_id],
            )
            if claim_info and claim_info.get("task") == "audit":
                raise HTTPException(403, "Find-and-replace is not available during an audit.")
            label = claim_info.get("label") if claim_info else None
            task = claim_info.get("task") if claim_info else None
            for number, expected, units, count in planned:
                previous = latest.get(number)
                reading_sha = content.reading_sha256(pages[number])
                # A page checked against an earlier reading is not checked against this one: a replace saves it
                # unchecked, so "the reading changed, check again" stays until someone looks (findings
                # 2026-11-16, item 23). A page checked against this reading keeps its status.
                stale = previous is not None and previous["reading_sha256"] != reading_sha
                try:
                    with db.begin_nested():
                        revision = store.save_page(
                            act_id,
                            number,
                            expected,
                            who["email"],
                            label=label,
                            task=task,
                            db=db,
                            reading_sha256=reading_sha,
                            units=units,
                            status="edited" if stale or not previous else previous["status"],
                            resolutions=content.standing(pages[number], previous["resolutions"])
                            if previous
                            else {},
                            comment=f"Replaced {find!r} with {replacement!r} ({count}×) across the Act"[
                                :2000
                            ],
                        )
                except Conflict:
                    conflicts.append(number)
                    continue
                changed.append({"page": number, "revision": revision, "replacements": count})
        payload: dict = {"changed": changed, "conflicts": conflicts}
        if claim_info and claim_info.get("implicit") and claim_info.get("claim_token"):
            wid = details_key(act)
            _evict_work(wid)
            payload["claim_token"] = claim_info["claim_token"]
            payload["claim_label"] = claim_info["label"]
            payload["claim_expires_at"] = claim_info["claim_expires_at"]
            payload["claim_work_id"] = wid
            payload["claim_documents"] = sorted(sibling_act_ids(act))
            return JSONResponse(payload, headers={"X-Claim-Token": claim_info["claim_token"]})
        return payload

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
                    "resolutions": content.standing(page, revision["resolutions"]) if revision else {},
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
        """The workbench's document for a work: in this language if it has it, else another; the work
        itself where it is one document (a Nigerian law has one text, under its own id)."""
        named = [f"{work}-{lang}" for lang in (language, "en", "fr", "rw") if lang]
        return next((found for found in [*named, work] if library.has(found)), None)

    def latest_in_force(act: dict) -> bool:
        """Whether the last law of the document's line of repeals is filed in force by its listing: a law
        no later text repeals is not thereby in force (of Rwanda's, most are not filed at all)."""
        latest = (act.get("currency") or {}).get("latest")
        found = document_of(latest["work"], act.get("language")) if latest else None
        return bool(found) and (library.listed(found).get("currency") or {}).get("status") == "in_force"

    def standing(act: dict) -> dict | None:
        """Whether the document is current, as its collection was prepared to tell: repealed by a later
        text (with the words of its provision that does, their page, and the last law of its line, said
        to be in force only where its listing files it so), as its country's official listing files it
        (named, where it has one: record_view.LISTINGS), or not yet known; what amends it, and whether
        the text here is known to be as published (a Gazette's; an agency's copy may be brought up to
        date); a law it was made under that is since repealed; and the instruments made under it."""
        current = act.get("currency")
        if not current:
            return None
        language = act.get("language")

        def named(item: dict) -> dict:
            quoted = {k: item[k] for k in ("words", "page") if item.get(k)}
            return {"title": item["title"], "id": document_of(item["work"], language), **quoted}

        out = {k: current[k] for k in ("status", "basis", "disagrees") if k in current}
        out["listing"] = record_view.LISTINGS.get(act.get("jurisdiction"))
        for key in ("repealed_by", "amended_by"):
            if current.get(key):
                out[key] = [named(item) for item in current[key]]
        if current.get("amended_by"):
            out["as_published"] = bool(act.get("issue"))
        if current.get("latest"):
            out["latest"] = named(current["latest"]) | {"in_force": latest_in_force(act)}
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
        # Each detail as read, with its page and words, beside its field; the legal facts are only ever
        # here, never in the first draft, until the reviewer chooses them.
        suggestions = (
            # A Nigerian law's details as read from its text, and whether a later text of its collection
            # repeals it, with that provision's words: suggested, as Rwanda's are.
            record_view.standing_suggestions(
                act,
                record_view.suggestions_view(details.suggest(reading, act, library.manifest_for(act_id))),
                latest_in_force(act),
            )
            if view["place"].code == "ng" and not record_view.reader_suggested(act)
            else record_view.suggestions(act, latest_in_force(act))
        )
        title = suggestions.get("identity.title")
        if cited_names.is_cited_here(act) and isinstance((title or {}).get("value"), str):
            # A Nigerian Act's short title as laws are cited; its words as printed stay its evidence.
            suggestions["identity.title"] = {**title, "value": cited_names.cited(title["value"])[0]}
        links = act.get("relations") or []
        if links:  # the laws it affects, as the collection's texts say: suggested, never filled in
            said = "the laws this text repeals or amends, and the later ones that repeal or amend it"
            read = suggestions.get("relations") or {}
            if view["place"].code == "ng" and isinstance(read.get("value"), list):
                # A Nigerian Act's long title names what it repeals too, with its page: kept, and what
                # the collection's texts say added to it, each link once.
                plain = lambda link: re.sub(r"[^a-z0-9]+", "", str(link.get("target", "")).lower())  # noqa: E731
                same = lambda a, b: a["kind"] == b["kind"] and (plain(a) in plain(b) or plain(b) in plain(a))  # noqa: E731
                more = [link for link in read["value"] if not any(same(link, held) for held in links)]
                suggestions["relations"] = {
                    **read,
                    "value": [*links, *more],
                    "evidence": f"{read.get('evidence') or 'Its text'}; and {said}",
                }
            else:
                suggestions["relations"] = {"value": links, "evidence": said}
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
            "notices": view.get("notices", []),
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

    @app.get("/api/documents/{act_id}/details/fields")
    def document_details_fields(request: Request, act_id: str, family: str = "", kind: str = ""):
        """The form fields for a family and kind at this document's place, read-only."""
        auth.current(request, store)
        act = act_or_404(act_id)
        if family not in R.FAMILIES or kind not in R.FAMILIES[family][1]:
            raise HTTPException(422, "Choose the document's family and a kind within it")
        _, profile = record_view.place_for(act, library.manifest_for(act_id), profiles)
        specs = record_view.fields(family, kind, profile)
        return view_schema({"family": family, "specs": specs})

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
        token = request.headers.get("X-Claim-Token")
        with store.engine.begin() as db:
            claim_info = claims.guard(
                db,
                act,
                who,
                token=token,
                write=True,
                act_id=act_id,
                approved_act_ids=sibling_act_ids(act),
            )
            if claim_info and claim_info.get("task") == "audit":
                raise HTTPException(
                    403, "Audit claims may only submit page audits, not change document details."
                )
            label = claim_info.get("label") if claim_info else None
            task = claim_info.get("task") if claim_info else None
            try:
                revision = store.save_details(
                    details_key(act),
                    int(body.get("expected_revision", -1)),
                    who["email"],
                    label=label,
                    task=task,
                    db=db,
                    details=given,
                    absent=absent,
                    status=status,
                    comment=str(body.get("comment", ""))[:2000],
                )
            except Conflict as error:
                raise HTTPException(409, str(error)) from error
        if claim_info and claim_info.get("implicit") and claim_info.get("claim_token"):
            wid = details_key(act)
            _evict_work(wid)
            return JSONResponse(
                {
                    "revision": revision,
                    "claim_token": claim_info["claim_token"],
                    "claim_label": claim_info["label"],
                    "claim_expires_at": claim_info["claim_expires_at"],
                    "claim_work_id": wid,
                    "claim_documents": sorted(sibling_act_ids(act)),
                },
                headers={"X-Claim-Token": claim_info["claim_token"]},
            )
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

    # --- multi-agent claims, labels, audits & admin report (§4, §7, §8, §10, §16) ----------------

    @app.post("/api/queue/claim-next")
    def claim_next_work(request: Request, body: dict = JSON_BODY):
        """Claim the next eligible work for review, status research, or audit (§4.6)."""
        who = auth.current(request, store, write=True, role="reviewer")
        account = who["email"].strip().lower()
        task = str(body.get("task") or "review").strip().lower()
        if task not in claims.TASKS:
            raise HTTPException(422, f"task must be one of {sorted(claims.TASKS)}")
        _claims_open(task)
        label = claims.validate_label(body.get("label"))
        jurisdiction = str(body.get("jurisdiction") or "").strip().lower()
        kind = str(body.get("kind") or "").strip().lower()
        raw_max_pages = body.get("max_pages")
        max_pages: int | None = None
        if raw_max_pages not in (None, ""):
            try:
                max_pages = int(raw_max_pages)
            except (TypeError, ValueError) as error:
                raise HTTPException(422, "max_pages must be a positive integer") from error
            if max_pages <= 0:
                raise HTTPException(422, "max_pages must be a positive integer")

        snapshot, evicted = _cached_work_snapshot()
        works_by_id = snapshot["works_by_id"]
        doc_to_work = snapshot["doc_to_work"]
        activity = snapshot["activity"]
        details_stamps = snapshot["details_stamps"]
        live_claims = snapshot["live_claims"]
        approved_works = snapshot["approved_works"]
        parked_works = snapshot["parked_works"]

        def matches(work: dict) -> bool:
            if jurisdiction and jurisdiction not in work["jurisdictions"]:
                return False
            if kind and kind not in work["kinds"]:
                return False
            return not (max_pages is not None and _work_pages(work) > max_pages)

        own_lapsed: list[str] = []
        candidates: list[str] = []
        drawn_by_work: dict[str, dict[str, list[int]]] = {}

        if task == "review":
            excluded = approved_works | parked_works | live_claims | evicted
            # Priority 1: caller's own lapsed work (§4.6).
            with store.engine.connect() as db:
                caller_page_acts = {
                    row.act_id
                    for row in db.execute(
                        select(page_revision.c.act_id)
                        .where(
                            page_revision.c.author == account,
                            page_revision.c.label == label,
                            page_revision.c.task == "review",
                        )
                        .distinct()
                    )
                }
                caller_detail_works = {
                    row.act_id
                    for row in db.execute(
                        select(detail_revision.c.act_id)
                        .where(
                            detail_revision.c.author == account,
                            detail_revision.c.label == label,
                            detail_revision.c.task == "review",
                        )
                        .distinct()
                    )
                }
                caller_works = {doc_to_work[a] for a in caller_page_acts if a in doc_to_work} | (
                    caller_detail_works & set(works_by_id)
                )
                for wid in sorted(caller_works):
                    if wid in excluded or not matches(works_by_id[wid]):
                        continue
                    closed = db.execute(
                        select(work_release.c.id)
                        .where(
                            work_release.c.work_id == wid,
                            work_release.c.outcome.in_(("done", "parked")),
                        )
                        .limit(1)
                    ).first()
                    if closed is not None:
                        continue
                    doc_ids = [d["id"] for d in works_by_id[wid]["docs"]]
                    other_page = db.execute(
                        select(page_revision.c.act_id)
                        .where(
                            page_revision.c.act_id.in_(doc_ids),
                            (page_revision.c.author != account)
                            | (page_revision.c.label != label)
                            | (page_revision.c.label.is_(None)),
                        )
                        .limit(1)
                    ).first()
                    if other_page is not None:
                        continue
                    other_detail = db.execute(
                        select(detail_revision.c.act_id)
                        .where(
                            detail_revision.c.act_id == wid,
                            (detail_revision.c.author != account)
                            | (detail_revision.c.label != label)
                            | (detail_revision.c.label.is_(None)),
                        )
                        .limit(1)
                    ).first()
                    if other_detail is not None:
                        continue
                    own_lapsed.append(wid)
            own_lapsed.sort(key=lambda wid: works_by_id[wid]["importance"])

            # Priority 2: fresh works in queue.importance order.
            fresh_works = [
                wid
                for wid, work in works_by_id.items()
                if wid not in excluded
                and wid not in own_lapsed
                and matches(work)
                and all(activity.get(d["id"]) is None for d in work["docs"])
                and details_stamps.get(wid) is None
            ]
            fresh_works.sort(key=lambda wid: works_by_id[wid]["importance"])
            candidates = fresh_works

            # Priority 3: others' lapsed works (only when enabled, and after 2h without a save).
            if os.environ.get("ALVARY_REVIEW_HAND_ON_LAPSED", "off").strip().lower() == "on":
                two_hours_ago = store_now() - timedelta(hours=2)
                seen_set = excluded | set(own_lapsed) | set(fresh_works)
                others_lapsed = []
                with store.engine.connect() as db:
                    for wid, work in works_by_id.items():
                        if wid in seen_set or not matches(work):
                            continue
                        latest_rel = db.execute(
                            select(work_release.c.outcome)
                            .where(work_release.c.work_id == wid)
                            .order_by(work_release.c.id.desc())
                            .limit(1)
                        ).scalar()
                        if latest_rel in ("done", "parked"):
                            continue
                        doc_ids = [d["id"] for d in work["docs"]]
                        last_page = db.execute(
                            select(func.max(page_revision.c.created_at)).where(
                                page_revision.c.act_id.in_(doc_ids)
                            )
                        ).scalar()
                        last_det = db.execute(
                            select(func.max(detail_revision.c.created_at)).where(
                                detail_revision.c.act_id == wid
                            )
                        ).scalar()
                        stamps = [_aware(t) for t in (last_page, last_det) if t is not None]
                        if stamps and max(stamps) <= two_hours_ago:
                            others_lapsed.append(wid)
                others_lapsed.sort(key=lambda wid: works_by_id[wid]["importance"])
                candidates.extend(others_lapsed)

        elif task == "status":
            may_change_approved = bool(who["operator"].get("may_change_approved"))
            excluded = (
                live_claims | evicted | parked_works | (set() if may_change_approved else approved_works)
            )
            with store.engine.connect() as db:
                latest_det = (
                    select(detail_revision.c.act_id, func.max(detail_revision.c.revision).label("rev"))
                    .group_by(detail_revision.c.act_id)
                    .subquery()
                )
                rows = db.execute(
                    select(detail_revision)
                    .join(
                        latest_det,
                        (detail_revision.c.act_id == latest_det.c.act_id)
                        & (detail_revision.c.revision == latest_det.c.rev),
                    )
                    .where(detail_revision.c.status == "confirmed")
                ).all()
            eligible = []
            for row in rows:
                wid = row.act_id
                if wid not in works_by_id or wid in excluded or not matches(works_by_id[wid]):
                    continue
                if status_research_pending(dict(row._mapping), works_by_id[wid]["docs"][0]):
                    eligible.append(wid)
            eligible.sort(key=lambda wid: works_by_id[wid]["importance"])
            candidates = eligible

        else:  # task == "audit"
            excluded = approved_works | live_claims | evicted
            open_audits = store.list_audits(open_only=True, limit=2000)
            for row in open_audits:
                if row["reviewer"] == account:
                    continue
                wid = row["work_id"]
                if wid not in works_by_id or wid in excluded or not matches(works_by_id[wid]):
                    continue
                drawn_by_work.setdefault(wid, {}).setdefault(row["act_id"], []).append(int(row["page"]))
            for per_doc in drawn_by_work.values():
                for aid in per_doc:
                    per_doc[aid] = sorted(set(per_doc[aid]))
            candidates = sorted(drawn_by_work, key=lambda wid: works_by_id[wid]["importance"])

        claimed = claims.claim_next(
            store,
            candidates,
            account,
            label,
            task=task,
            own_lapsed=own_lapsed,
            on_evict=_evict_work,
        )
        return _claim_response(
            claimed,
            pages_drawn_by_doc=drawn_by_work.get(claimed["work_id"]) if task == "audit" else None,
        )

    @app.post("/api/claims/{work_id}")
    def claim_named_work(request: Request, work_id: str, body: dict = JSON_BODY):
        """Claim one named work, or rotate/re-issue the token if the same (account, label) holds it."""
        who = auth.current(request, store, write=True, role="reviewer")
        if not work_documents(work_id):
            raise HTTPException(404, "No such work")
        task = str(body.get("task") or "review").strip().lower()
        if task not in claims.TASKS:
            raise HTTPException(422, f"task must be one of {sorted(claims.TASKS)}")
        _claims_open(task)
        label = claims.validate_label(body.get("label"))
        account = who["email"].strip().lower()
        person = who["operator"]
        if person.get("agent"):
            # By name an agent account reaches only what "Claim next" would hand it.
            approved, parked = _work_standing(work_id)
            if approved and not (task == "status" and person.get("may_change_approved")):
                raise claims.ClaimError(403, claims.MSG_APPROVED, "approved_guard")
            if parked:
                raise claims.ClaimError(409, claims.MSG_LEFT_FOR_OWNER, "claim_none")
        if task == "audit":
            # An audit is another account's reading of the drawn pages: the reviewer's own account is
            # never told which pages were drawn, and cannot hold them back from an auditor.
            reviewers = {
                row["reviewer"] for row in store.list_audits(work_id=work_id, open_only=True, limit=500)
            }
            if not reviewers - {account}:
                raise claims.ClaimError(404, claims.MSG_NOTHING_TO_CLAIM, "claim_none")
        claimed = claims.claim_work(store, work_id, account, label, task=task)
        _evict_work(work_id)
        pages_drawn: dict[str, list[int]] | None = None
        if claimed["task"] == "audit":
            pages_drawn = {}
            for row in store.list_audits(work_id=work_id, open_only=True, limit=500):
                pages_drawn.setdefault(row["act_id"], []).append(int(row["page"]))
            for aid in pages_drawn:
                pages_drawn[aid] = sorted(set(pages_drawn[aid]))
        return _claim_response(claimed, pages_drawn_by_doc=pages_drawn)

    @app.post("/api/claims/{work_id}/renew")
    def renew_work_claim(request: Request, work_id: str, body: dict | None = OPTIONAL_JSON_BODY):
        """The tab's heartbeat: whether this browser still holds the work, and until when (claims.renew_claim)."""
        who = auth.current(request, store, write=True, role="reviewer")
        _claims_open()
        token = request.headers.get("X-Claim-Token") or str((body or {}).get("token") or "")
        return claims.renew_claim(store, work_id, who["email"], token)

    def _draw_audit(work_id: str, released: dict) -> dict:
        """Draw the audit's pages for a review just released as done: how many, or why none. The audit is
        off until ALVARY_REVIEW_AUDIT_RATE is set above 0; switched on, a draw that is skipped is written to
        the log, so a work that was never audited can be found."""
        cfg = audit_mod.audit_config()
        if cfg["rate"] <= 0:
            return {"drawn": 0, "skipped": "audit_off"}

        def skipped(reason: str) -> dict:
            store.audit(
                released["account"],
                "audit_draw_skipped",
                detail={"work_id": work_id, "release_id": released["release_id"], "reason": reason},
            )
            return {"drawn": 0, "skipped": reason}

        if claims.claims_mode() != "enforce":
            return skipped("claims_not_enforced")
        docs = work_documents(work_id)
        if _work_standing(work_id)[0]:
            return skipped("approved_document")
        if not docs:
            return skipped("no_documents")
        doc_pages: dict[str, list[int]] = {}
        latest_by_doc: dict[str, dict[int, dict]] = {}
        for doc in docs:
            if doc.get("text_held"):
                continue
            reading = library.reading(doc["id"])
            page_nums = [int(p["page"]) for p in reading["pages"]]
            if page_nums:
                doc_pages[doc["id"]] = page_nums
                latest_by_doc[doc["id"]] = store.latest_pages(doc["id"])
        drawn = audit_mod.draw_pages(doc_pages, rate=cfg["rate"], minimum=cfg["minimum"])
        for aid, page_num in drawn:
            rev = (latest_by_doc.get(aid, {}).get(page_num) or {}).get("revision", 0)
            store.create_audit_draw(
                work_id=work_id,
                act_id=aid,
                page=page_num,
                release_id=released["release_id"],
                reviewer=released["account"],
                reviewer_label=released["label"],
                reviewer_revision=int(rev),
            )
        return {"drawn": len(drawn)}

    @app.post("/api/claims/{work_id}/release")
    def release_work_claim(request: Request, work_id: str, body: dict = JSON_BODY):
        """Release a work claim (done | parked | abandoned) and draw audit pages on review done."""
        who = auth.current(request, store, write=True)
        if not (who["operator"]["roles"] & {"reviewer", "approver", "admin"}):
            raise HTTPException(403, "This needs the reviewer, approver, or admin role")
        outcome = str(body.get("outcome") or "done").strip().lower()
        reason = str(body.get("reason") or "")
        token = request.headers.get("X-Claim-Token") or body.get("token")
        label = body.get("label")
        released = claims.release_claim(
            store, work_id, who, outcome=outcome, reason=reason, token=token, label=label
        )
        _invalidate_work_cache()

        if released["outcome"] == "done" and released["task"] == "review":
            try:
                released["audit"] = _draw_audit(work_id, released)
            except Exception as error:  # noqa: BLE001 - the release is saved: a draw that failed is said, not hidden
                reason = f"error: {error_said(error)}"
                store.audit(
                    released["account"],
                    "audit_draw_skipped",
                    detail={"work_id": work_id, "release_id": released["release_id"], "reason": reason},
                )
                released["audit"] = {"drawn": 0, "skipped": reason}
        return released

    @app.get("/api/claims")
    def active_claims(request: Request):
        """List all live claims (admin only)."""
        auth.current(request, store, role="admin")
        return {"claims": claims.list_claims(store, include_expired=False)}

    @app.post("/api/labels/block")
    def block_agent_label(request: Request, body: dict = JSON_BODY):
        """Pause a specific agent label (admin + fresh passkey)."""
        who = privileged.fresh(auth.current(request, store, write=True, role="admin"))
        account = str(body.get("account") or "").strip().lower()
        label = str(body.get("label") or "").strip()
        reason = str(body.get("reason") or "")
        if not account:
            raise HTTPException(422, "account is required")
        return claims.block_label(store, account, label, blocked_by=who["email"], reason=reason)

    @app.post("/api/labels/unblock")
    def unblock_agent_label(request: Request, body: dict = JSON_BODY):
        """Unpause a previously blocked agent label (admin + fresh passkey)."""
        who = privileged.fresh(auth.current(request, store, write=True, role="admin"))
        account = str(body.get("account") or "").strip().lower()
        label = str(body.get("label") or "").strip()
        if not account or not label:
            raise HTTPException(422, "account and label are required")
        unblocked = claims.unblock_label(store, account, label, actor=who["email"])
        return {"ok": True, "unblocked": unblocked}

    @app.post("/api/audits/{audit_id}/resolve")
    def resolve_review_audit(request: Request, audit_id: int, body: dict = JSON_BODY):
        """Resolve a submitted review audit (accepted_auditor | kept_reviewer); admin/approver + passkey."""
        who = auth.current(request, store, write=True)
        if not (who["operator"]["roles"] & {"admin", "approver"}):
            raise HTTPException(403, "This needs the approver or admin role")
        privileged.fresh(who)
        decision = str(body.get("decision") or "").strip()
        comment = str(body.get("comment") or "")[:2000]
        try:
            resolved = store.resolve_audit(
                audit_id, decision=decision, decided_by=who["email"], comment=comment
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        return _serialize_audit_row(resolved)

    @app.post("/api/agents/pause-all")
    def pause_all_agent_accounts(request: Request):
        """Deactivate every active agent account and end all of their live sessions (admin + passkey)."""
        who = privileged.fresh(auth.current(request, store, write=True, role="admin"))
        paused = store.pause_all_agents(actor=who["email"])
        return {"ok": True, "paused": paused, "count": len(paused)}

    @app.get("/api/agents/report")
    def agents_report(request: Request, days: int = 1):
        """Admin dashboard report: live claims, page counts, pace, parked, audit windows & material diffs.

        Built from each save's word count, never its text, and over a day unless asked (a week at most):
        with 150 agents saving all day the text of a week's pages would not fit in a copy's memory."""
        auth.current(request, store, role="admin")
        days = max(1, min(days, 7))
        moment = store_now()
        since = moment - timedelta(days=days)
        today_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)

        live_claims = claims.list_claims(store, include_expired=False)

        with store.engine.connect() as db:
            blocked_rows = db.execute(select(blocked_label).order_by(blocked_label.c.blocked_at.desc())).all()
            blocked_labels = [
                {
                    "account": r.account,
                    "label": r.label,
                    "blocked_at": _aware(r.blocked_at).isoformat(),
                    "blocked_by": r.blocked_by,
                    "reason": r.reason,
                }
                for r in blocked_rows
            ]

            rev_rows = db.execute(
                select(
                    page_revision.c.act_id,
                    page_revision.c.page,
                    page_revision.c.author,
                    page_revision.c.label,
                    page_revision.c.task,
                    page_revision.c.words,
                    page_revision.c.created_at,
                )
                .where(page_revision.c.created_at >= since)
                .order_by(page_revision.c.created_at.asc())
            ).all()

            parked_rows = db.execute(
                select(work_release)
                .where(work_release.c.outcome == "parked", work_release.c.released_at >= since)
                .order_by(work_release.c.released_at.desc())
            ).all()

            completed_audits = db.execute(
                select(
                    review_audit.c.reviewer,
                    review_audit.c.reviewer_label,
                    review_audit.c.verdict,
                    review_audit.c.owner_decision,
                )
                .where(
                    review_audit.c.audited_at.is_not(None),
                    review_audit.c.verdict.is_not(None),
                )
                .order_by(review_audit.c.audited_at.asc(), review_audit.c.id.asc())
            ).all()

        revisions = [
            {
                "act_id": r.act_id,
                "page": r.page,
                "author": r.author,
                "label": r.label or "",
                "task": r.task,
                "words": r.words,  # None for a page saved before words were counted: never "fast"
                "created_at": _aware(r.created_at),
            }
            for r in rev_rows
        ]
        counts_map: dict[tuple[str, str], dict] = {}
        for rev in revisions:
            if rev["task"] == "audit":
                continue
            key = (rev["author"], rev["label"])
            bucket = counts_map.setdefault(
                key,
                {"account": rev["author"], "label": rev["label"], "pages_saved": 0, "today_pages_saved": 0},
            )
            bucket["pages_saved"] += 1
            if rev["created_at"] >= today_start:
                bucket["today_pages_saved"] += 1
        page_counts = sorted(counts_map.values(), key=lambda c: (c["account"], c["label"]))

        # Each label's figures, and as evidence its ten fastest full pages, not every save it made.
        pace = [
            {
                **row,
                "intervals": sorted(
                    (gap for gap in row["intervals"] if gap["fast"]), key=lambda gap: gap["seconds"]
                )[:10],
            }
            for row in audit_mod.pace_report(revisions)
        ]

        parked_counts_map: dict[tuple[str, str], int] = {}
        parked_recent = []
        for r in parked_rows:
            key = (r.account, r.label)
            parked_counts_map[key] = parked_counts_map.get(key, 0) + 1
            parked_recent.append(
                {
                    "id": r.id,
                    "work_id": r.work_id,
                    "account": r.account,
                    "label": r.label,
                    "task": r.task,
                    "reason": r.reason,
                    "released_at": _aware(r.released_at).isoformat(),
                }
            )
        parked_counts = [
            {"account": acc, "label": lbl, "count": cnt}
            for (acc, lbl), cnt in sorted(parked_counts_map.items())
        ]

        # A material page the owner decided for the reviewer does not count against the reviewer: there
        # the auditor was the one wrong. One the owner has not decided yet counts, and is said apart.
        verdicts_by_label: dict[tuple[str, str], list[str]] = {}
        undecided_by_label: dict[tuple[str, str], int] = {}
        for r in completed_audits:
            who_reviewed = (r.reviewer, r.reviewer_label)
            verdicts = verdicts_by_label.setdefault(who_reviewed, [])
            if r.owner_decision == "kept_reviewer":
                continue
            verdicts.append(r.verdict)
            if r.verdict == "material" and not r.owner_decision:
                undecided_by_label[who_reviewed] = undecided_by_label.get(who_reviewed, 0) + 1
        audit_windows = [
            {
                "account": acc,
                "label": lbl,
                **audit_mod.evaluate_window(verdicts),
                "material_undecided": undecided_by_label.get((acc, lbl), 0),
            }
            for (acc, lbl), verdicts in sorted(verdicts_by_label.items())
        ]

        open_material_raw = store.list_audits(verdict="material", unresolved_only=True, limit=200)
        open_material = []
        with store.engine.connect() as db:
            for item in open_material_raw:
                rev_units = db.execute(
                    select(page_revision.c.units).where(
                        page_revision.c.act_id == item["act_id"],
                        page_revision.c.page == item["page"],
                        page_revision.c.revision == item["reviewer_revision"],
                    )
                ).scalar()
                serialized = _serialize_audit_row(item)
                serialized["reviewer_units"] = rev_units or []
                open_material.append(serialized)

        return {
            "days": days,
            "claims": live_claims,
            "blocked_labels": blocked_labels,
            "page_counts": page_counts,
            "pace": pace,
            "parked": {
                "counts": parked_counts,
                "recent": parked_recent[:100],
            },
            "audit_windows": audit_windows,
            "open_material_audits": open_material,
        }

    editorial.install(app, store, library, review)
    citations.install(app, store, library, review)
    privileged.install(app, store, relying)
    access_section.install(app, store, access, relying)
    traffic_section.install(app, store, access)
    operators_section.install(app, store, relying)
    publishing_section.install(app, store, library, state, relying)

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
    claims.check_settings()
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
