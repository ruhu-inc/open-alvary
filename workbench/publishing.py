"""Publishing in the workbench: where each document stands in public view, what the next run will do,
the runs so far, and a run asked for now.

Publishing itself is the publish job (operations.publish run): every night (ALVARY_PUBLISH_SCHEDULE),
or when an administrator asks here after a passkey check. The workbench never writes the public
catalogue: it asks for a run (a row the job takes up) and starts the job through Cloud Run's API
(ALVARY_PUBLISH_JOB, the job's full name), as its own service account, which may start that job and
nothing else. What a run did is in the review database's publishing tables (workbench.store), written
by the publisher's own login; what the next run would do is worked out here from the approvals and the
owner's decisions, as the run itself decides it.
"""

import os
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from workbench import auth
from workbench.privileged import fresh, refuse

READERS = {"approver", "admin"}  # who sees the Publishing page
RUNNER = "admin"  # who may ask for a run now (with a passkey check)
COUNTRIES = {"rw": "Rwanda", "ng": "Nigeria"}
TEXT_WORDS = {
    "public": "Text public",
    "held": "Text held: not published",
    "not_covered": "Text not covered by the text decision",
    "no_decision": "Text not published: no text decision",
    "blocked": "Text not published",
    "withdrawn": "Text withdrawn",
    "waiting": "Text waits for approval",
}


def schedule() -> str:
    return os.environ.get("ALVARY_PUBLISH_SCHEDULE", "every night at 01:30 UTC")


def job() -> str | None:
    return os.environ.get("ALVARY_PUBLISH_JOB") or None


def decisions(jurisdiction: str) -> dict:
    """The owner's two decisions for a country, or why there is none (operations.publish)."""
    from operations import publish

    out: dict = {}
    try:
        found = publish.current_decision(jurisdiction)
        out["catalogue"] = {"until": found["expires_at"][:10], "by": found["decided_by"]}
    except publish.Refused as refused:
        out["catalogue"] = {"refused": str(refused)}
    try:
        found = publish.current_text_decision(jurisdiction)
        out["text"] = {
            "until": found["expires_at"][:10],
            "by": found["decided_by"],
            "types": found["document_types"],
        }
    except publish.Refused as refused:
        out["text"] = {"refused": str(refused)}
    return out


def kind_of(now: dict) -> str:
    """The text decision's kind for a document: its details' family, as publishing reads it."""
    from operations import publish

    return publish.DOCUMENT_TYPES.get((now.get("view") or {}).get("family"), "other")


def standing(act: dict, now: dict, status: dict | None, decided: dict) -> dict:
    """Where one document stands in public view, and what the next run will do with it, in words:
    {"state", "words", "text"}. `now` is its review state (workbench.review), `status` what the latest
    run recorded, `decided` its country's decisions."""
    head = now.get("head") or {}
    approved = bool(now.get("approved"))
    public = bool(status and status["details"] == "public")
    since = status and status.get("details_since")
    if not approved:
        if public:
            return {
                "state": "taken_down_next",
                "words": f"Public since {_day(since)}. Its approval is no longer current: the next run takes it "
                "down unless it is approved again.",
                "text": None,
            }
        if status and status["details"] == "taken_down":
            return {
                "state": "taken_down",
                "words": "Taken down: its approval lapsed or was revoked.",
                "text": None,
            }
        return {"state": "not_approved", "words": "", "text": None}
    if "refused" in decided["catalogue"]:
        return {
            "state": "blocked",
            "words": f"Approved, but not published: {decided['catalogue']['refused']}",
            "text": None,
        }
    approved_at = head.get("created_at")
    pending = not public or (approved_at and status and _after(approved_at, status["updated_at"]))
    text = _text_words(act, kind_of(now), status, decided, pending)
    if pending:
        return {
            "state": "next_run",
            "words": f"Approved: goes public at the next run ({schedule()}).",
            "text": text,
        }
    return {"state": "public", "words": f"Public since {_day(since)}.", "text": text}


def _text_words(act: dict, kind: str, status: dict | None, decided: dict, pending: bool) -> str:
    if act.get("text_held"):
        return "Text held: words are masked in the source; only the details are published."
    if "refused" in decided["text"]:
        return f"Text not published: {decided['text']['refused']}"
    if kind not in decided["text"]["types"]:
        covered = ", ".join(decided["text"]["types"])
        return f"Text not published: the text decision covers {covered} only."
    if status and status["text"] == "public" and not pending:
        return f"Text public since {_day(status.get('text_since'))}."
    if status and status["text"] in ("blocked", "not_covered") and status.get("reason") and not pending:
        return f"Text not published: {status['reason']}"
    return "Text goes public at the next run."


def short(row_approval: str | None, status: dict | None, decided: dict) -> str | None:
    """A document's publication in one word for the list: public, next_run, blocked, taken_down_next."""
    public = bool(status and status["details"] == "public")
    if row_approval == "approved":
        if "refused" in decided["catalogue"]:
            return "blocked"
        return "public" if public else "next_run"
    if public:
        return "taken_down_next"
    return None


def _day(value) -> str:
    if value is None:
        return "the latest run"
    return (value if isinstance(value, datetime) else datetime.fromisoformat(str(value))).date().isoformat()


def _after(first, second) -> bool:
    def aware(value):
        value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    return aware(first) > aware(second)


def start_job(name: str) -> None:
    """Start the publish job now, as this service's own account (Cloud Run's API)."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    response = AuthorizedSession(credentials).post(
        f"https://run.googleapis.com/v2/{name}:run", json={}, timeout=30
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Cloud Run refused to start the publish job ({response.status_code})")


def install(app: FastAPI, store, library, state, relying) -> None:
    """The Publishing page's API. `state(act)` is the review state (workbench.review)."""

    def reader(request: Request, *, write: bool = False) -> dict:
        found = auth.current(request, store, write=write)
        if not found["operator"]["roles"] & READERS:
            refuse(403, "role_forbidden", "Publishing is for approvers and administrators.")
        return found

    def runs() -> list[dict]:
        out = []
        for run in store.publication_runs(10):
            names = store.names([run["requested_by"]]) if run["requested_by"] else {}
            out.append(
                {
                    **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in run.items()},
                    "requested_by_name": names.get((run["requested_by"] or "").lower()),
                }
            )
        return out

    @app.get("/api/publishing")
    def overview(request: Request):
        found = reader(request)
        statuses = store.publication_statuses()
        countries: dict[str, dict] = {}
        for act_id in sorted(store.approved_ids()):
            if not library.has(act_id):
                continue
            act = library.act(act_id)
            code = act.get("jurisdiction") or ""
            if act.get("part") or not code:
                continue
            country = countries.setdefault(
                code,
                {
                    "code": code,
                    "name": COUNTRIES.get(code, code.upper()),
                    "decisions": decisions(code),
                    "documents": [],
                },
            )
            now = state(act)
            where = standing(act, now, statuses.get(act_id), country["decisions"])
            if where["state"] == "not_approved":
                continue
            country["documents"].append({"id": act_id, "title": act.get("title") or act_id, **where})
        recent = runs()
        waiting = next((r for r in recent if r["status"] in ("requested", "running")), None)
        return {
            "schedule": schedule(),
            "configured": job() is not None,
            "can_run": job() is not None and relying is not None and RUNNER in found["operator"]["roles"],
            "waiting": waiting,
            "runs": recent,
            "countries": [countries[code] for code in sorted(countries)],
        }

    @app.post("/api/publishing/run")
    def run_now(request: Request):
        found = auth.current(request, store, write=True, role=RUNNER)
        fresh(found)  # a passkey check in the last 10 minutes (workbench/privileged.py)
        name = job()
        if name is None or relying is None:
            refuse(
                503, "publishing_off", "No publish job is set up for this workbench: the deployment sets it."
            )
        if any(r["status"] in ("requested", "running") for r in store.publication_runs(5)):
            refuse(
                409,
                "run_waiting",
                "A run is already waiting or running: its result appears here when it ends.",
            )
        number = store.request_publication(found["email"])
        try:
            start_job(name)  # looked up here, so a test can stand in for Cloud Run
        except Exception as error:  # noqa: BLE001 - the request stays: the nightly run takes it up
            refuse(502, "start_failed", f"{error}. Your request is kept: the next nightly run takes it up.")
        return {"run": number}
