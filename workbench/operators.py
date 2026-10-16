"""Operators: who may use the workbench, managed by its administrators (role "admin").

An administrator adds a person (their own Google address, a Workspace or a personal one), sets their
roles, deactivates or reactivates them, and issues a one-time code with which they register a passkey
(shown once, to the administrator, to pass on). Every change needs the administrator's passkey checked
in the last 10 minutes (workbench/privileged.py) and is in the workbench's audit log. Nobody can
deactivate themselves, and the last active administrator keeps the role.

Reaching the workbench at all also takes Google's IAP gate: the operators' Google group (runbook), or a
per-person grant (deployment/gcp/workbench.sh operator).
"""

import os
import re

from fastapi import FastAPI, Request

from workbench import auth
from workbench.passkeys import RelyingParty
from workbench.privileged import JSON_BODY, PRIVILEGED, fresh, refuse
from workbench.store import ROLES, Store

ROLE = "admin"
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def install(app: FastAPI, store: Store, relying: RelyingParty | None) -> None:
    def administrator(request: Request, *, write: bool = False) -> dict:
        if relying is None:
            refuse(404, "operators_off", "Operator management needs the workbench's https address.")
        found = auth.current(request, store, write=write, role=ROLE)
        return fresh(found) if write else found

    def other_admins(email: str) -> int:
        return sum(1 for o in store.operators() if o["active"] and ROLE in o["roles"] and o["email"] != email)

    def target(email: str) -> dict:
        found = next((o for o in store.operators() if o["email"] == email.strip().lower()), None)
        if found is None:
            refuse(404, "not_found", "No operator with that email.")
        return found

    @app.get("/api/operators")
    def listing(request: Request):
        administrator(request)
        # The Google group IAP admits (runbook), named so administrators know where else to add people.
        return {
            "operators": store.operators(),
            "roles": sorted(ROLES),
            "iap_group": os.environ.get("ALVARY_REVIEW_IAP_GROUP"),
        }

    @app.post("/api/operators")
    def save(request: Request, body: dict = JSON_BODY):
        """Add a person, or change a person's name and roles."""
        found = administrator(request, write=True)
        email = str(body.get("email") or "").strip().lower()
        name = " ".join(str(body.get("name") or "").split())
        roles = {str(r) for r in body.get("roles") or []}
        if not EMAIL.match(email):
            refuse(400, "invalid_input", "Give the person's Google account email.")
        if not 1 <= len(name) <= 200:
            refuse(400, "invalid_input", "Give the person's name.")
        if not roles or not roles <= ROLES:
            refuse(400, "invalid_input", f"Choose roles among: {', '.join(sorted(ROLES))}.")
        existing = next((o for o in store.operators() if o["email"] == email), None)
        if existing and ROLE in existing["roles"] and ROLE not in roles and not other_admins(email):
            refuse(409, "last_admin", "This is the last administrator: make someone else one first.")
        store.add_operator(email, name, roles, actor=found["email"])
        return {"ok": True, "added": existing is None}

    @app.post("/api/operators/{email}/active")
    def activation(request: Request, email: str, body: dict = JSON_BODY):
        found = administrator(request, write=True)
        person = target(email)
        if body.get("active"):
            store.reactivate(person["email"], actor=found["email"])
            return {"ok": True}
        if person["email"] == found["email"]:
            refuse(409, "self", "You cannot deactivate yourself.")
        if ROLE in person["roles"] and not other_admins(person["email"]):
            refuse(409, "last_admin", "This is the last administrator: make someone else one first.")
        store.deactivate(person["email"], actor=found["email"])
        return {"ok": True}

    @app.post("/api/operators/{email}/enrolment")
    def enrolment(request: Request, email: str, body: dict = JSON_BODY):
        """A one-time code (24 hours) for the person to register a passkey; with replace, their
        passkeys are revoked first (one was lost). Shown once, here."""
        found = administrator(request, write=True)
        person = target(email)
        if not person["active"]:
            refuse(409, "inactive", "Reactivate this operator first.")
        if not set(person["roles"]) & PRIVILEGED:
            refuse(409, "no_passkey_needed", "Only administrators and access administrators use passkeys.")
        if body.get("replace"):
            for key in store.passkeys(person["email"]):
                store.revoke_passkey(person["email"], key["credential_id"])
        elif person["passkeys"]:
            refuse(409, "has_passkey", "This person has a passkey already; replace it only if it was lost.")
        return {"ok": True, "code": store.issue_enrolment(person["email"], actor=found["email"])}
