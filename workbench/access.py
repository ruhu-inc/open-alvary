"""The workbench's Access section (MAC-11): operator access actions on the accounts schema.

Only operators with the access_admin role reach it. Every change also needs the session's CSRF token
and a passkey check within the last 10 minutes (workbench/privileged.py). The accounts database is
reached through its limited role (accounts.access_admin): ALVARY_ACCESS_DATABASE_URL. Without it, or
without the workbench's https address to bind passkeys to, the section is off.
"""

import os
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from accounts.access_admin import AccessAdmin, Action
from accounts.store import AccountError, Context
from workbench import auth
from workbench.passkeys import RelyingParty
from workbench.privileged import JSON_BODY, fresh, refuse
from workbench.store import Store

ROLE = "access_admin"


def install(app: FastAPI, store: Store, admin: AccessAdmin | None, relying: RelyingParty | None) -> None:
    def operator(request: Request, *, write: bool = False) -> dict:
        if admin is None or relying is None:
            refuse(404, "access_off", "The Access section is not set up on this workbench.")
        return auth.current(request, store, write=write, role=ROLE)

    def changing(request: Request) -> dict:
        return fresh(operator(request, write=True))

    def action(found: dict, body: dict) -> Action:
        evidence = str(body.get("evidence") or "").strip() or None
        try:
            return Action(
                found["email"],
                str(body.get("reason") or ""),
                evidence,
                Context(client_family="review workbench"),
            )
        except AccountError as error:
            refuse(error.status, error.code, error.message)

    def run(call):
        try:
            return call()
        except AccountError as error:
            refuse(error.status, error.code, error.message)

    # --- reading ------------------------------------------------------------------------------

    @app.get("/api/access/requests")
    def requests(request: Request):
        operator(request)
        return run(admin.requests)

    @app.get("/api/access/tiers")
    def tiers(request: Request):
        operator(request)
        return [
            {"id": t["id"], "name": t["name"], "countries": t["allowance"].get("jurisdictions")}
            for t in admin.accounts.tiers(published_only=False)
        ]

    @app.get("/api/access/search")
    def search(request: Request, q: str = ""):
        operator(request)
        return run(lambda: admin.search(q))

    @app.get("/api/access/organisations/{organisation_id}")
    def organisation(request: Request, organisation_id: str):
        operator(request)
        return run(lambda: admin.organisation(organisation_id))

    @app.get("/api/access/audit")
    def audit(
        request: Request,
        organisation: str | None = None,
        action: str | None = None,
        before: int | None = None,
    ):
        operator(request)
        return run(lambda: admin.audit(organisation_id=organisation, action=action, before=before, limit=200))

    @app.get("/api/access/clients")
    def clients(request: Request):
        operator(request)
        return run(admin.verified_clients)

    # --- changes: role, CSRF and a fresh passkey ----------------------------------------------

    @app.post("/api/access/organisations/{organisation_id}/grant")
    def grant(request: Request, organisation_id: str, body: dict = JSON_BODY):
        found = changing(request)
        until = body.get("until")
        try:
            ends = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=UTC) if until else None
        except (TypeError, ValueError):
            refuse(400, "invalid_input", "Give the end date as YYYY-MM-DD.")
        jurisdictions = body.get("jurisdictions")
        entitlement = run(
            lambda: admin.grant(
                action(found, body),
                organisation_id,
                tier=body.get("tier") or None,
                jurisdictions=jurisdictions if isinstance(jurisdictions, list) and jurisdictions else None,
                until=ends,
                waiver=bool(body.get("waiver")),
                request_id=body.get("request_id") or None,
            )
        )
        return {"ok": True, "entitlement": entitlement}

    @app.post("/api/access/organisations/{organisation_id}/requests/{request_id}")
    def decide(request: Request, organisation_id: str, request_id: str, body: dict = JSON_BODY):
        found = changing(request)
        decision = body.get("decision")
        if decision not in ("declined", "needs_information"):
            refuse(400, "invalid_input", "Decline, or ask for more information.")
        run(lambda: admin.decide(action(found, body), organisation_id, request_id, decision))
        return {"ok": True}

    @app.post("/api/access/organisations/{organisation_id}/revoke")
    def revoke(request: Request, organisation_id: str, body: dict = JSON_BODY):
        found = changing(request)
        run(lambda: admin.revoke(action(found, body), organisation_id))
        return {"ok": True}

    @app.post("/api/access/organisations/{organisation_id}/state")
    def organisation_state(request: Request, organisation_id: str, body: dict = JSON_BODY):
        found = changing(request)
        state = run(
            lambda: admin.set_organisation(action(found, body), organisation_id, str(body.get("change")))
        )
        return {"ok": True, "state": state}

    @app.post("/api/access/people/{person_id}/state")
    def person_state(request: Request, person_id: str, body: dict = JSON_BODY):
        found = changing(request)
        run(lambda: admin.set_person(action(found, body), person_id, str(body.get("change"))))
        return {"ok": True}

    @app.post("/api/access/organisations/{organisation_id}/owner")
    def owner(request: Request, organisation_id: str, body: dict = JSON_BODY):
        found = changing(request)
        run(lambda: admin.restore_owner(action(found, body), organisation_id, str(body.get("membership_id"))))
        return {"ok": True}

    @app.post("/api/access/credentials/revoke")
    def revoke_credential(request: Request, body: dict = JSON_BODY):
        found = changing(request)
        # A leaked key is parsed for its ID only: never logged or kept.
        return {
            "ok": True,
            **run(lambda: admin.revoke_credential(action(found, body), str(body.get("credential")))),
        }

    @app.post("/api/access/clients")
    def pin(request: Request, body: dict = JSON_BODY):
        found = changing(request)
        run(
            lambda: admin.pin(
                action(found, body),
                str(body.get("client_id") or ""),
                str(body.get("name") or ""),
                str(body.get("publisher") or ""),
            )
        )
        return {"ok": True}

    @app.post("/api/access/clients/unpin")
    def unpin(request: Request, body: dict = JSON_BODY):
        found = changing(request)
        run(lambda: admin.unpin(action(found, body), str(body.get("client_id") or "")))
        return {"ok": True}


def from_environment() -> AccessAdmin | None:
    """The Access section's accounts connection, when configured."""
    url = os.environ.get("ALVARY_ACCESS_DATABASE_URL")
    if not url:
        return None
    from sqlalchemy import create_engine

    from accounts import mail
    from accounts.store import Accounts
    from rights.database import PolicyNamespace

    engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
    namespace = PolicyNamespace(os.environ.get("ALVARY_ACCESS_NAMESPACE", "public"))
    return AccessAdmin(Accounts(engine, namespace), mail.Mailer.from_environment())
