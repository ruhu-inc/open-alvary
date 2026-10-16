"""Passkeys for the workbench's privileged sections (MAC-11): Access (access_admin) and Operators
(admin).

An operator holding either role registers a passkey (the first with a one-time enrolment code from an
administrator) and confirms it in the session; every privileged change then needs that check within
the last 10 minutes, the session's CSRF token and the role. Passkeys are bound to the workbench's own
https address (ALVARY_REVIEW_BASE_URL), and checked by workbench/passkeys.py.
"""

import hashlib
import os
from datetime import timedelta

from fastapi import Body, FastAPI, HTTPException, Request

from workbench import auth
from workbench.passkeys import PasskeyError, RelyingParty, b64url
from workbench.store import Store, now

FRESH = timedelta(minutes=10)
PRIVILEGED = {"admin", "access_admin"}
JSON_BODY = Body(...)


def refuse(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def fresh(found: dict) -> dict:
    """The session's passkey check is recent enough for a change."""
    checked = found.get("passkey_at")
    if checked is None or now() - checked > FRESH:
        refuse(403, "passkey_required", "Confirm with your passkey to continue.")
    return found


def relying_from_environment(dev_login: bool) -> RelyingParty | None:
    origin = os.environ.get("ALVARY_REVIEW_BASE_URL") or ("http://localhost:8090" if dev_login else None)
    return RelyingParty(origin.rstrip("/")) if origin else None


def install(app: FastAPI, store: Store, relying: RelyingParty | None) -> None:
    def holder(request: Request, *, write: bool = False) -> dict:
        if relying is None:
            refuse(
                404, "passkeys_off", "Passkeys need the workbench's https address (ALVARY_REVIEW_BASE_URL)."
            )
        found = auth.current(request, store, write=write)
        if not found["operator"]["roles"] & PRIVILEGED:
            refuse(403, "role_forbidden", "Passkeys are for administrators and access administrators.")
        return found

    def handle(email: str) -> str:
        return b64url(hashlib.sha256(("open-alvary-operator:" + email).encode()).digest())

    @app.get("/api/passkeys/me")
    def me(request: Request):
        found = holder(request)
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

    @app.post("/api/passkeys/options")
    def options(request: Request, body: dict = JSON_BODY):
        found = holder(request, write=True)
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

    @app.post("/api/passkeys")
    def register(request: Request, body: dict = JSON_BODY):
        found = holder(request, write=True)
        email = found["email"]
        name = " ".join(str(body.get("name") or "Passkey").split())[:100] or "Passkey"
        challenge = store.take_challenge(found["token_hash"], "register")
        if challenge is None:
            refuse(400, "challenge_expired", "That request expired. Try again.")
        if store.passkeys(email):
            # A further passkey needs a passkey check first.
            fresh(found)
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

    @app.post("/api/passkeys/verify")
    def verify(request: Request, body: dict = JSON_BODY):
        found = holder(request, write=True)
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

    @app.delete("/api/passkeys/{credential_id}")
    def remove(request: Request, credential_id: str):
        found = fresh(holder(request, write=True))
        if not store.revoke_passkey(found["email"], credential_id):
            refuse(404, "not_found", "No such passkey.")
        return {"ok": True}
