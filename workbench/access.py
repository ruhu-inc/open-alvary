"""The workbench's Access section (MAC-11): operator access actions on the accounts schema.

Only operators with the access_admin role reach it. Every change also needs the session's CSRF
token and a passkey check in this session within the last 10 minutes (workbench/passkeys.py); the
first passkey is registered with a one-time enrolment code from the server. The accounts database
is reached through its limited role (accounts.access_admin): ALVARY_ACCESS_DATABASE_URL. Without
it, or without the workbench's https address to bind passkeys to, the section is off.
"""

import hashlib
import os
from datetime import UTC, datetime, timedelta

from fastapi import Body, FastAPI, HTTPException, Request

from accounts.access_admin import AccessAdmin, Action
from accounts.store import AccountError, Context
from workbench import auth
from workbench.passkeys import PasskeyError, RelyingParty, b64url
from workbench.store import Store, now

ROLE = "access_admin"
FRESH = timedelta(minutes=10)
JSON_BODY = Body(...)


def refuse(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def install(app: FastAPI, store: Store, admin: AccessAdmin | None, relying: RelyingParty | None) -> None:
    def operator(request: Request, *, write: bool = False) -> dict:
        if admin is None or relying is None:
            refuse(404, "access_off", "The Access section is not set up on this workbench.")
        return auth.current(request, store, write=write, role=ROLE)

    def changing(request: Request) -> dict:
        found = operator(request, write=True)
        checked = found.get("passkey_at")
        if checked is None or now() - checked > FRESH:
            refuse(403, "passkey_required", "Confirm with your passkey to continue.")
        return found

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

    def handle(email: str) -> str:
        return b64url(hashlib.sha256(("open-alvary-operator:" + email).encode()).digest())

    # --- passkeys -----------------------------------------------------------------------------

    @app.get("/api/access/me")
    def me(request: Request):
        found = operator(request)
        keys = store.passkeys(found["email"])
        checked = found.get("passkey_at")
        return {
            "email": found["email"],
            "passkeys": [
                {
                    "id": k["credential_id"],
                    "name": k["name"],
                    "created_at": k["created_at"],
                    "last_used_at": k["last_used_at"],
                }
                for k in keys
            ],
            "needs_enrolment": not keys,
            "fresh_until": (checked + FRESH).isoformat() if checked and now() - checked <= FRESH else None,
        }

    @app.post("/api/access/passkeys/options")
    def options(request: Request, body: dict = JSON_BODY):
        found = operator(request, write=True)
        purpose = body.get("purpose")
        keys = [k["credential_id"] for k in store.passkeys(found["email"])]
        if purpose == "register":
            challenge = store.begin_challenge(found["token_hash"], "register")
            return relying.creation_options(challenge, found["email"], handle(found["email"]), keys)
        if purpose == "verify":
            if not keys:
                refuse(409, "no_passkey", "Register a passkey first.")
            return relying.request_options(store.begin_challenge(found["token_hash"], "verify"), keys)
        refuse(400, "invalid_input", "Register or verify.")

    @app.post("/api/access/passkeys")
    def register(request: Request, body: dict = JSON_BODY):
        found = operator(request, write=True)
        email = found["email"]
        name = " ".join(str(body.get("name") or "Passkey").split())[:100] or "Passkey"
        challenge = store.take_challenge(found["token_hash"], "register")
        if challenge is None:
            refuse(400, "challenge_expired", "That request expired. Try again.")
        if store.passkeys(email):
            # A further passkey needs a passkey check first.
            checked = found.get("passkey_at")
            if checked is None or now() - checked > FRESH:
                refuse(403, "passkey_required", "Confirm with an existing passkey first.")
        try:
            record = relying.register(body.get("credential") or {}, challenge)
        except PasskeyError as error:
            refuse(400, "passkey_refused", str(error))
        if not store.passkeys(email) and not store.take_enrolment(email, str(body.get("code") or "")):
            store.audit(email, "passkey_enrolment_refused")
            refuse(403, "enrolment_required", "Enter the enrolment code you were given (one use, 24 hours).")
        store.add_passkey(email, record, name)
        store.passkey_checked(found["token_hash"], email, record["credential_id"], record["sign_count"])
        return {"ok": True}

    @app.post("/api/access/passkeys/verify")
    def verify(request: Request, body: dict = JSON_BODY):
        found = operator(request, write=True)
        credential = body.get("credential") or {}
        challenge = store.take_challenge(found["token_hash"], "verify")
        if challenge is None:
            refuse(400, "challenge_expired", "That request expired. Try again.")
        stored = next(
            (k for k in store.passkeys(found["email"]) if k["credential_id"] == credential.get("id")), None
        )
        if stored is None:
            refuse(403, "passkey_refused", "That passkey is not registered to you here.")
        try:
            counter = relying.verify(credential, challenge, stored)
        except PasskeyError as error:
            store.audit(found["email"], "passkey_refused", detail={"reason": str(error)})
            refuse(403, "passkey_refused", str(error))
        store.passkey_checked(found["token_hash"], found["email"], stored["credential_id"], counter)
        return {"ok": True, "fresh_until": (now() + FRESH).isoformat()}

    @app.delete("/api/access/passkeys/{credential_id}")
    def remove(request: Request, credential_id: str):
        found = changing(request)
        if not store.revoke_passkey(found["email"], credential_id):
            refuse(404, "not_found", "No such passkey.")
        return {"ok": True}

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


def from_environment(dev_login: bool) -> tuple[AccessAdmin | None, RelyingParty | None]:
    """The Access section's accounts connection and passkey binding, when configured."""
    url = os.environ.get("ALVARY_ACCESS_DATABASE_URL")
    origin = os.environ.get("ALVARY_REVIEW_BASE_URL") or ("http://localhost:8090" if dev_login else None)
    if not url or not origin:
        return None, None
    from sqlalchemy import create_engine

    from accounts import mail
    from accounts.store import Accounts
    from rights.database import PolicyNamespace

    engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
    namespace = PolicyNamespace(os.environ.get("ALVARY_ACCESS_NAMESPACE", "public"))
    return AccessAdmin(Accounts(engine, namespace), mail.Mailer.from_environment()), RelyingParty(
        origin.rstrip("/")
    )
