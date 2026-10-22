"""Account operations on the accounts schema (MAC-03–05, MAC-13), one transaction each.

Every change checks the caller's role in that transaction, writes an audit entry, and leaves the
invariants the database itself guards (last owner, access periods, append-only logs) to it.
"""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from accounts import keys, policies, roles
from accounts.ids import new_id, valid
from rights.database import PolicyNamespace

TIERS = Path(__file__).with_name("tiers.json")


def listed_plans() -> list[dict]:
    """The plans in accounts/tiers.json that people may see and ask for: published, with a listing
    action. Plans granted only by agreement (pilot, partner, sovereign) are never listed."""
    return [
        t for t in json.loads(TIERS.read_text()) if t.get("published") and t.get("listing", {}).get("action")
    ]


COUNTRY = re.compile(r"^[a-z]{2}$")  # a plan's countries: ISO 3166-1 alpha-2, lower case
COUNTRY_CHANGE_DAYS = 30


SESSION_DAYS = 30
IDLE_DAYS = 7
ORGANISATIONS_PER_PERSON = 5
PERSONAL_KEYS_PER_MEMBER = 10
SERVICE_KEYS_PER_ORGANISATION = 20
KINDS = ("individual", "company", "academic", "non_profit", "government", "other")


class AccountError(Exception):
    """A refusal the console shows: a stable code, an HTTP status and what to do."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


@dataclass(frozen=True)
class Context:
    """Where a change came from, for the audit log (truncated, never a full address)."""

    network_prefix: str | None = None
    client_family: str | None = None
    request_id: str | None = None


def canonical_email(value: str) -> str:
    """ACS-02: trim; IDNA and lower-case the domain; keep the local part exactly."""
    value = (value or "").strip()
    local, at, domain = value.rpartition("@")
    if not at or not local or not domain:
        raise AccountError("invalid_input", 400, "Enter a valid email address.")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise AccountError("invalid_input", 400, "Enter a valid email address.") from error
    return f"{local}@{domain}"


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _sqlstate(error: DBAPIError) -> tuple[str | None, str]:
    orig = getattr(error, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(orig, "sqlstate", None), (getattr(diag, "message_primary", None) or str(orig))


class Accounts:
    def __init__(self, engine: Engine, namespace: PolicyNamespace, key_pepper: bytes | None = None):
        self.engine = engine
        self.namespace = namespace
        self._pepper = key_pepper

    # ---------- plumbing ----------
    def _t(self, conn: Connection, name: str) -> str:
        return self.namespace.qualified(conn, "accounts", name)

    def _fn(self, conn: Connection, name: str) -> str:
        return self.namespace.qualified(conn, "policy", name)

    def _audit(
        self,
        conn: Connection,
        actor: tuple[str, str],
        action: str,
        *,
        organisation: str | None = None,
        target: tuple[str, str] | None = None,
        summary: dict | None = None,
        context: Context | None = None,
    ) -> None:
        context = context or Context()
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'audit_event')} (id, actor_type, actor_ref, organisation_id,"
                " action, target_type, target_id, summary, network_prefix, client_family, request_id)"
                " VALUES (:id, :actor_type, :actor_ref, :organisation, :action, :target_type, :target_id,"
                " CAST(:summary AS jsonb), :network, :client, :request)"
            ),
            {
                "id": new_id("evt"),
                "actor_type": actor[0],
                "actor_ref": actor[1],
                "organisation": organisation,
                "action": action,
                "target_type": target[0] if target else None,
                "target_id": target[1] if target else None,
                "summary": json.dumps(summary or {}),
                "network": context.network_prefix,
                "client": context.client_family,
                "request": context.request_id,
            },
        )

    def _actor(self, conn: Connection, person_id: str) -> tuple[str, str]:
        ref = conn.scalar(
            text(f"SELECT id FROM {self._t(conn, 'actor')} WHERE person_id = :p"), {"p": person_id}
        )
        return ("person", ref)

    def _membership(self, conn: Connection, person_id: str, organisation_id: str, *, lock=False) -> dict:
        row = (
            conn.execute(
                text(
                    f"SELECT m.id, m.role, o.state AS organisation_state FROM {self._t(conn, 'membership')} m"
                    f" JOIN {self._t(conn, 'organisation')} o ON o.id = m.organisation_id"
                    " WHERE m.person_id = :p AND m.organisation_id = :o AND o.state <> 'deleted'"
                    + (" FOR UPDATE OF o" if lock else "")
                ),
                {"p": person_id, "o": organisation_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            # Same answer whether the organisation exists or not.
            raise AccountError("not_found", 404, "No such organisation, or you are not a member.")
        return dict(row)

    def _require(self, membership: dict, action: str) -> None:
        if not roles.can(membership["role"], action):
            raise AccountError("role_forbidden", 403, "Your role in this organisation does not allow this.")

    def access(self, organisation_id: str, conn: Connection | None = None) -> dict:
        if conn is None:
            with self.engine.connect() as conn:
                return self.access(organisation_id, conn)
        access = conn.scalar(text(f"SELECT {self._fn(conn, 'accounts_access')}(:o)"), {"o": organisation_id})
        if access and access.get("tier"):
            access["tier"]["name"] = conn.scalar(
                text(f"SELECT name FROM {self._t(conn, 'tier')} WHERE id = :t"), {"t": access["tier"]["id"]}
            )
        return access

    # ---------- the runtime's view ----------
    def check_credential(self, request: dict) -> dict:
        """The narrow database check (check_credential): organisation, credential, tier and scope."""
        with self.engine.begin() as conn:
            return conn.scalar(
                text(f"SELECT {self._fn(conn, 'check_credential')}(CAST(:r AS jsonb))"),
                {"r": json.dumps(request)},
            )

    def record_usage(self, entry: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(f"SELECT {self._fn(conn, 'record_usage')}(CAST(:e AS jsonb))"), {"e": json.dumps(entry)}
            )

    def credential(self, organisation_id: str, credential: str) -> dict | None:
        """The credential the gate has just checked, as REST v1's GET /key describes it: an API key's ID,
        kind, name, creation, expiry and whether a rotation replaced it, or a connected app's grant. Only
        that credential, read within its own organisation; never a secret or a hash."""
        with self.engine.connect() as conn:
            if credential.startswith("key_"):
                keys_table = self._t(conn, "api_key")
                row = conn.execute(
                    text(
                        f"SELECT 'key_' || k.key_id AS id, k.kind, k.name, k.created_at, k.expires_at,"
                        f" EXISTS (SELECT 1 FROM {keys_table} n WHERE n.organisation_id = k.organisation_id"
                        f" AND n.rotated_from = k.key_id) AS replaced FROM {keys_table} k"
                        " WHERE k.key_id = :k AND k.organisation_id = :o"
                    ),
                    {"k": credential.removeprefix("key_"), "o": organisation_id},
                )
            else:
                row = conn.execute(
                    text(
                        "SELECT g.id, 'app' AS kind, g.client_name AS name, g.created_at,"
                        " NULL::timestamptz AS expires_at, false AS replaced"
                        f" FROM {self._t(conn, 'client_grant')} g WHERE g.id = :g AND g.organisation_id = :o"
                    ),
                    {"g": credential, "o": organisation_id},
                )
            found = row.mappings().first()
            return dict(found) if found else None

    # ---------- who to tell ----------
    def notice_recipients(self, organisation_id: str, request_id: str | None = None) -> dict:
        """The organisation's name and who hears about its access: the request's contact, else its owners."""
        with self.engine.connect() as conn:
            name = conn.scalar(
                text(f"SELECT name FROM {self._t(conn, 'organisation')} WHERE id = :o"),
                {"o": organisation_id},
            )
            contact = (
                conn.scalar(
                    text(
                        f"SELECT contact_email FROM {self._t(conn, 'access_request')} WHERE id = :r AND organisation_id = :o"
                    ),
                    {"r": request_id, "o": organisation_id},
                )
                if request_id
                else None
            )
            owners = conn.scalars(
                text(
                    f"SELECT p.email FROM {self._t(conn, 'membership')} m JOIN {self._t(conn, 'person')} p"
                    " ON p.id = m.person_id WHERE m.organisation_id = :o AND m.role = 'owner' AND p.state = 'active'"
                ),
                {"o": organisation_id},
            ).all()
        return {"organisation": name, "to": [contact] if contact else sorted(set(owners))}

    def invitation_notice(self, invitation_id: str) -> dict:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        f"SELECT i.email, i.role, o.name AS organisation, coalesce(a.display_name, 'A member') AS inviter"
                        f" FROM {self._t(conn, 'invitation')} i JOIN {self._t(conn, 'organisation')} o"
                        f" ON o.id = i.organisation_id JOIN {self._t(conn, 'membership')} m ON m.id = i.invited_by"
                        f" LEFT JOIN {self._t(conn, 'actor')} a ON a.person_id = m.person_id WHERE i.id = :i"
                    ),
                    {"i": invitation_id},
                )
                .mappings()
                .one()
            )
            return dict(row)

    # ---------- people and sign-in ----------
    def sign_in(
        self, *, issuer: str, subject: str, method: str, email: str, label: str, context=None
    ) -> dict:
        """The person for a provider identity, created at its first sign-in. Never matched by email
        (MAC-08): an email already used by another account is refused, not linked."""
        email = canonical_email(email)
        with self.engine.begin() as conn:
            person = (
                conn.execute(
                    text(
                        f"SELECT p.id, p.state, i.id AS identity FROM {self._t(conn, 'identity')} i"
                        f" JOIN {self._t(conn, 'person')} p ON p.id = i.person_id"
                        " WHERE i.issuer = :issuer AND i.subject = :subject"
                    ),
                    {"issuer": issuer, "subject": subject},
                )
                .mappings()
                .first()
            )
            if person is not None:
                if person["state"] != "active":
                    raise AccountError("account_inactive", 403, "This account is suspended or closed.")
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'identity')} SET last_used_at = statement_timestamp() WHERE id = :i"
                    ),
                    {"i": person["identity"]},
                )
                self._audit(
                    conn,
                    self._actor(conn, person["id"]),
                    "session.signed_in",
                    context=context,
                    summary={"method": method},
                )
                return {"id": person["id"], "created": False}
            taken = conn.scalar(
                text(
                    f"SELECT EXISTS(SELECT 1 FROM {self._t(conn, 'person')} WHERE email = :e AND state <> 'closed')"
                ),
                {"e": email},
            )
            if taken:
                raise AccountError(
                    "email_in_use",
                    409,
                    "An account already uses this email. Sign in the way you did before, then add this"
                    " sign-in method under Security.",
                )
            person_id, actor_id = new_id("acc"), new_id("act")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'person')} (id, email, email_verified_at)"
                    " VALUES (:id, :email, statement_timestamp())"
                ),
                {"id": person_id, "email": email},
            )
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'actor')} (id, person_id, display_name) VALUES (:a, :p, :n)"
                ),
                {"a": actor_id, "p": person_id, "n": email},
            )
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'identity')} (id, person_id, method, issuer, subject, label,"
                    " last_used_at) VALUES (:id, :p, :m, :issuer, :subject, :label, statement_timestamp())"
                ),
                {
                    "id": new_id("idn"),
                    "p": person_id,
                    "m": method,
                    "issuer": issuer,
                    "subject": subject,
                    "label": label[:320],
                },
            )
            self._audit(
                conn, ("person", actor_id), "account.created", context=context, summary={"method": method}
            )
            return {"id": person_id, "created": True}

    def link_identity(self, person_id: str, *, issuer, subject, method, label, context=None) -> None:
        with self.engine.begin() as conn:
            owner = conn.scalar(
                text(f"SELECT person_id FROM {self._t(conn, 'identity')} WHERE issuer = :i AND subject = :s"),
                {"i": issuer, "s": subject},
            )
            if owner == person_id:
                return
            if owner is not None:
                raise AccountError("identity_in_use", 409, "That sign-in already belongs to another account.")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'identity')} (id, person_id, method, issuer, subject, label)"
                    " VALUES (:id, :p, :m, :i, :s, :l)"
                ),
                {
                    "id": new_id("idn"),
                    "p": person_id,
                    "m": method,
                    "i": issuer,
                    "s": subject,
                    "l": label[:320],
                },
            )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "identity.linked",
                context=context,
                summary={"method": method},
            )

    def person_for_identity(self, issuer: str, subject: str) -> str | None:
        with self.engine.connect() as conn:
            return conn.scalar(
                text(f"SELECT person_id FROM {self._t(conn, 'identity')} WHERE issuer = :i AND subject = :s"),
                {"i": issuer, "s": subject},
            )

    def identities(self, person_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT id, method, label, linked_at, last_used_at FROM {self._t(conn, 'identity')}"
                    " WHERE person_id = :p ORDER BY linked_at"
                ),
                {"p": person_id},
            )
            return [dict(r) for r in rows.mappings()]

    def unlink_identity(self, person_id: str, identity_id: str, context=None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(f"SELECT id FROM {self._t(conn, 'person')} WHERE id = :p FOR UPDATE"), {"p": person_id}
            )
            count = conn.scalar(
                text(f"SELECT count(*) FROM {self._t(conn, 'identity')} WHERE person_id = :p"),
                {"p": person_id},
            )
            if count <= 1:
                raise AccountError("last_identity", 409, "Keep at least one way to sign in.")
            method = conn.scalar(
                text(
                    f"DELETE FROM {self._t(conn, 'identity')} WHERE id = :i AND person_id = :p RETURNING method"
                ),
                {"i": identity_id, "p": person_id},
            )
            if method is None:
                raise AccountError("not_found", 404, "No such sign-in method.")
            self._audit(
                conn,
                self._actor(conn, person_id),
                "identity.unlinked",
                context=context,
                summary={"method": method},
            )

    def person(self, person_id: str) -> dict:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        f"SELECT id, revision, email, display_name, state, second_factor, created_at"
                        f" FROM {self._t(conn, 'person')} WHERE id = :p"
                    ),
                    {"p": person_id},
                )
                .mappings()
                .one()
            )
            accepted = conn.execute(
                text(
                    f"SELECT document, version FROM {self._t(conn, 'policy_acceptance')} WHERE person_id = :p"
                ),
                {"p": person_id},
            ).all()
        accepted = {(d, v) for d, v in accepted}
        person = dict(row)
        person["policies_pending"] = [d for d, v in policies.CURRENT.items() if (d, v) not in accepted]
        return person

    def accept_policies(self, person_id: str, versions: dict, context=None) -> None:
        if versions != policies.CURRENT:
            raise AccountError(
                "policy_changed", 409, "The terms changed while you were reading. Review them again."
            )
        with self.engine.begin() as conn:
            for document, version in versions.items():
                conn.execute(
                    text(
                        f"INSERT INTO {self._t(conn, 'policy_acceptance')} (person_id, document, version)"
                        " VALUES (:p, :d, :v) ON CONFLICT DO NOTHING"
                    ),
                    {"p": person_id, "d": document, "v": version},
                )
            self._audit(
                conn, self._actor(conn, person_id), "policy.accepted", context=context, summary=versions
            )

    def update_profile(self, person_id: str, display_name: str, context=None) -> None:
        name = " ".join((display_name or "").split())
        if not 1 <= len(name) <= 100:
            raise AccountError("invalid_input", 400, "Enter a name of 1 to 100 characters.")
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'person')} SET display_name = :n, revision = revision + 1,"
                    " updated_at = statement_timestamp() WHERE id = :p"
                ),
                {"n": name, "p": person_id},
            )
            conn.execute(
                text(f"UPDATE {self._t(conn, 'actor')} SET display_name = :n WHERE person_id = :p"),
                {"n": name, "p": person_id},
            )

    def welcome(self, person_id: str, display_name: str, organisation_name: str, context=None) -> str:
        """First sign-in: name the person and make their own organisation (once, never recreated)."""
        if self.person(person_id)["policies_pending"]:
            raise AccountError(
                "policy_required", 409, "Accept the terms of service and privacy notice first."
            )
        self.update_profile(person_id, display_name, context)
        with self.engine.begin() as conn:
            existing = conn.scalar(
                text(f"SELECT id FROM {self._t(conn, 'organisation')} WHERE personal_of = :p"),
                {"p": person_id},
            )
            if existing:
                return existing
            return self._create_organisation(
                conn, person_id, organisation_name, "individual", None, True, context
            )

    def _create_organisation(self, conn, person_id, name, kind, country, personal, context) -> str:
        name = " ".join((name or "").split())
        if not 1 <= len(name) <= 120:
            raise AccountError("invalid_input", 400, "Enter an organisation name of 1 to 120 characters.")
        if kind not in KINDS or (country is not None and not (len(country) == 2 and country.isalpha())):
            raise AccountError(
                "invalid_input", 400, "Choose an organisation type and country from the lists."
            )
        conn.execute(
            text(f"SELECT id FROM {self._t(conn, 'person')} WHERE id = :p FOR UPDATE"), {"p": person_id}
        )
        made = conn.scalar(
            text(
                f"SELECT count(*) FROM {self._t(conn, 'organisation')} WHERE created_by = :p AND state <> 'deleted'"
            ),
            {"p": person_id},
        )
        if made >= ORGANISATIONS_PER_PERSON:
            raise AccountError("limit_reached", 409, "You can create up to 5 organisations. Ask us for more.")
        organisation_id = new_id("org")
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'organisation')} (id, name, kind, country, personal_of, created_by)"
                " VALUES (:id, :name, :kind, :country, :personal, :p)"
            ),
            {
                "id": organisation_id,
                "name": name,
                "kind": kind,
                "country": country and country.upper(),
                "personal": person_id if personal else None,
                "p": person_id,
            },
        )
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'membership')} (id, organisation_id, person_id, role)"
                " VALUES (:id, :o, :p, 'owner')"
            ),
            {"id": new_id("mem"), "o": organisation_id, "p": person_id},
        )
        self._audit(
            conn,
            self._actor(conn, person_id),
            "organisation.created",
            organisation=organisation_id,
            target=("organisation", organisation_id),
            context=context,
        )
        return organisation_id

    def create_organisation(self, person_id, name, kind="other", country=None, context=None) -> str:
        with self.engine.begin() as conn:
            return self._create_organisation(conn, person_id, name, kind, country, False, context)

    # ---------- console sessions and sign-in flows ----------
    def create_session(
        self, person_id: str, *, second_factor: bool, client_family=None, network_prefix=None
    ) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'console_session')} (token_hash, person_id, csrf, expires_at,"
                    " authenticated_at, second_factor_at, client_family, network_prefix) VALUES (:h, :p, :c,"
                    f" statement_timestamp() + interval '{SESSION_DAYS} days', statement_timestamp(),"
                    " CASE WHEN :mfa THEN statement_timestamp() END, :client, :network)"
                ),
                {
                    "h": token_hash(token),
                    "p": person_id,
                    "c": csrf,
                    "mfa": second_factor,
                    "client": client_family,
                    "network": network_prefix,
                },
            )
        return token, csrf

    def resume(self, token: str) -> dict | None:
        """The live session for a cookie, or None; idle sessions end after 7 days."""
        if not token or len(token) > 100:
            return None
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'console_session')} s SET last_seen_at = statement_timestamp()"
                        f" FROM {self._t(conn, 'person')} p WHERE s.token_hash = :h AND p.id = s.person_id"
                        " AND s.revoked_at IS NULL AND s.expires_at > statement_timestamp()"
                        f" AND s.last_seen_at > statement_timestamp() - interval '{IDLE_DAYS} days'"
                        " AND p.state = 'active'"
                        " RETURNING s.person_id, s.csrf, s.authenticated_at, s.second_factor_at,"
                        " statement_timestamp() AS now"
                    ),
                    {"h": token_hash(token)},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def refresh_session(self, token: str, *, second_factor: bool) -> None:
        """A fresh sign-in in the same session (MAC-08): the 10-minute window starts again."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'console_session')} SET authenticated_at = statement_timestamp(),"
                    " second_factor_at = CASE WHEN :mfa THEN statement_timestamp() ELSE second_factor_at END"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"h": token_hash(token), "mfa": second_factor},
            )

    def end_session(self, token: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'console_session')} SET revoked_at = statement_timestamp()"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"h": token_hash(token or "")},
            )

    def sign_in_failed(
        self, code: str, purpose: str, person_id: str | None = None, method: str | None = None, context=None
    ) -> None:
        """MAC-13.7: a sign-in that failed after a real start (a live, single-use flow), so stray
        requests cannot fill the log. The email tried is never recorded; the person is, when the
        identity was already theirs."""
        with self.engine.begin() as conn:
            ref = person_id and conn.scalar(
                text(f"SELECT id FROM {self._t(conn, 'actor')} WHERE person_id = :p"), {"p": person_id}
            )
            self._audit(
                conn,
                ("system", "sign-in"),
                "session.sign_in_failed",
                target=("actor", ref) if ref else None,
                summary={"code": code[:40], "purpose": purpose, **({"method": method} if method else {})},
                context=context,
            )

    def end_all_sessions(self, person_id: str, context=None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'console_session')} SET revoked_at = statement_timestamp()"
                    " WHERE person_id = :p AND revoked_at IS NULL"
                ),
                {"p": person_id},
            )
            self._audit(conn, self._actor(conn, person_id), "session.ended_everywhere", context=context)

    def sessions(self, person_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT created_at, last_seen_at, client_family, network_prefix, encode(token_hash, 'hex')"
                    f" AS ref FROM {self._t(conn, 'console_session')} WHERE person_id = :p AND revoked_at IS NULL"
                    " AND expires_at > statement_timestamp() ORDER BY last_seen_at DESC LIMIT 20"
                ),
                {"p": person_id},
            )
            return [dict(r) | {"ref": r["ref"][:12]} for r in rows.mappings()]

    def begin_flow(self, purpose: str, return_to: str, session: str | None = None) -> dict:
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        with self.engine.begin() as conn:
            conn.execute(
                text(f"DELETE FROM {self._t(conn, 'sign_in_flow')} WHERE expires_at < statement_timestamp()")
            )
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'sign_in_flow')} (state_hash, browser_hash, nonce, verifier,"
                    " purpose, session_hash, return_to) VALUES (:s, :b, :n, :v, :purpose, :session, :r)"
                ),
                {
                    "s": token_hash(state),
                    "b": token_hash(browser),
                    "n": nonce,
                    "v": verifier,
                    "purpose": purpose,
                    "session": token_hash(session) if session else None,
                    "r": return_to,
                },
            )
        return {"state": state, "browser": browser, "nonce": nonce, "verifier": verifier}

    def take_flow(self, state: str, browser: str) -> dict | None:
        """The flow this browser started, once, within 10 minutes."""
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        f"DELETE FROM {self._t(conn, 'sign_in_flow')} WHERE state_hash = :s"
                        " RETURNING browser_hash, nonce, verifier, purpose, session_hash, return_to,"
                        " expires_at > statement_timestamp() AS live"
                    ),
                    {"s": token_hash(state or "")},
                )
                .mappings()
                .first()
            )
        if (
            row is None
            or not row["live"]
            or not secrets.compare_digest(bytes(row["browser_hash"]), token_hash(browser or ""))
        ):
            return None
        return dict(row)

    # ---------- organisations ----------
    def organisations(self, person_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        f"SELECT o.id, o.name, o.kind, o.country, o.website, o.state, o.personal_of IS NOT NULL AS"
                        f" personal, m.role, m.id AS membership FROM {self._t(conn, 'membership')} m"
                        f" JOIN {self._t(conn, 'organisation')} o ON o.id = m.organisation_id"
                        " WHERE m.person_id = :p AND o.state <> 'deleted' ORDER BY o.personal_of IS NULL, o.name"
                    ),
                    {"p": person_id},
                )
                .mappings()
                .all()
            )
            return [dict(r) | {"access": self.access(r["id"], conn)} for r in rows]

    def organisation(self, person_id: str, organisation_id: str) -> dict:
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            row = (
                conn.execute(
                    text(
                        f"SELECT id, revision, name, kind, country, website, state, personal_of IS NOT NULL AS"
                        f" personal, created_at, updated_at FROM {self._t(conn, 'organisation')} WHERE id = :o"
                    ),
                    {"o": organisation_id},
                )
                .mappings()
                .one()
            )
            result = dict(row) | {"role": membership["role"], "access": self.access(organisation_id, conn)}
            if roles.can(membership["role"], "audit.read"):
                result["counts"] = dict(
                    conn.execute(
                        text(
                            f"SELECT (SELECT count(*) FROM {self._t(conn, 'membership')} WHERE organisation_id = :o)"
                            f" AS members, (SELECT count(*) FROM {self._t(conn, 'invitation')}"
                            " WHERE organisation_id = :o AND state = 'pending') AS pending_invitations"
                        ),
                        {"o": organisation_id},
                    )
                    .mappings()
                    .one()
                )
            return result

    def update_organisation(
        self, person_id, organisation_id, revision: int, changes: dict, context=None
    ) -> None:
        allowed = {"name", "kind", "country", "website"}
        if not changes or set(changes) - allowed:
            raise AccountError("invalid_input", 400, "Only name, type, country and website can change.")
        if "name" in changes:
            changes["name"] = " ".join((changes["name"] or "").split())
            if not 1 <= len(changes["name"]) <= 120:
                raise AccountError("invalid_input", 400, "Enter an organisation name of 1 to 120 characters.")
        if "kind" in changes and changes["kind"] not in KINDS:
            raise AccountError("invalid_input", 400, "Choose an organisation type from the list.")
        if changes.get("country") and not (len(changes["country"]) == 2 and changes["country"].isalpha()):
            raise AccountError("invalid_input", 400, "Choose a country from the list.")
        if changes.get("website") and not str(changes["website"]).startswith("https://"):
            raise AccountError("invalid_input", 400, "The website must start with https://.")
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "organisation.update")
            sets = ", ".join(f"{k} = :{k}" for k in changes)
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'organisation')} SET {sets}, revision = revision + 1,"
                    " updated_at = statement_timestamp() WHERE id = :o AND revision = :rev"
                ),
                {
                    **{k: (v.upper() if k == "country" and v else v or None) for k, v in changes.items()},
                    "o": organisation_id,
                    "rev": revision,
                },
            ).rowcount
            if not done:
                raise AccountError(
                    "revision_conflict", 409, "Someone else changed this. Reload and try again."
                )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "organisation.updated",
                organisation=organisation_id,
                target=("organisation", organisation_id),
                summary={"fields": sorted(changes)},
                context=context,
            )

    # ---------- access ----------
    def access_details(self, person_id: str, organisation_id: str) -> dict:
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            owner = membership["role"] == "owner"
            history = (
                conn.execute(
                    text(
                        f"SELECT e.id, e.tier_id, t.name AS tier_name, e.source, e.scope, e.starts_at, e.ends_at,"
                        f" e.state, e.reason FROM {self._t(conn, 'entitlement')} e JOIN {self._t(conn, 'tier')} t"
                        " ON t.id = e.tier_id WHERE e.organisation_id = :o ORDER BY e.starts_at DESC LIMIT 20"
                    ),
                    {"o": organisation_id},
                )
                .mappings()
                .all()
            )
            requests = (
                conn.execute(
                    text(
                        f"SELECT id, revision, purpose, expected_volume, scope_requested, reduced, tier_requested, contact_name,"
                        f" contact_email, state, decision, renewable_after, created_at, updated_at"
                        f" FROM {self._t(conn, 'access_request')} WHERE organisation_id = :o"
                        " ORDER BY created_at DESC LIMIT 10"
                    ),
                    {"o": organisation_id},
                )
                .mappings()
                .all()
            )
            return {
                "current": self.access(organisation_id, conn),
                "country_change_after": self._country_change_after(conn, organisation_id),
                "history": [dict(h) | ({} if owner else {"reason": None}) for h in history],
                "requests": [dict(r) for r in requests] if owner else [],
            }

    def request_access(self, person_id: str, organisation_id: str, request: dict, context=None) -> str:
        purpose = (request.get("purpose") or "").strip()
        volume = (request.get("expected_volume") or "").strip()
        scope = request.get("scope") or {"all": True}
        if not 1 <= len(purpose) <= 2000 or not 1 <= len(volume) <= 1000:
            raise AccountError(
                "invalid_input", 400, "Describe your use (up to 2,000 characters) and expected volume."
            )
        if scope != {"all": True} and not (
            isinstance(scope.get("jurisdictions"), list)
            and 1 <= len(scope["jurisdictions"]) <= 100
            and set(scope) == {"jurisdictions"}
            and all(isinstance(j, str) and j.isalnum() and len(j) <= 16 for j in scope["jurisdictions"])
        ):
            raise AccountError("invalid_input", 400, "Choose all jurisdictions or a list of them.")
        contact_name = " ".join((request.get("contact_name") or "").split())
        contact_email = canonical_email(request.get("contact_email") or "")
        if not 1 <= len(contact_name) <= 100:
            raise AccountError("invalid_input", 400, "Enter a contact name.")
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "access.request")
            blocked = conn.scalar(
                text(
                    f"SELECT max(renewable_after) FROM {self._t(conn, 'access_request')} WHERE organisation_id = :o"
                    " AND state = 'declined' AND renewable_after > statement_timestamp()"
                ),
                {"o": organisation_id},
            )
            if blocked and not request.get("new_information"):
                raise AccountError(
                    "renewal_too_soon",
                    409,
                    f"You can ask again after {blocked:%-d %B %Y}, or sooner if you have new information.",
                )
            plan = request.get("tier")
            if plan is not None and not (
                isinstance(plan, str)
                and conn.scalar(
                    text(f"SELECT published FROM {self._t(conn, 'tier')} WHERE id = :t AND id <> 'starter'"),
                    {"t": plan},
                )
            ):
                raise AccountError("invalid_input", 400, "Choose one of the published plans.")
            if plan is not None:
                scope = self._plan_scope(conn, plan, scope)
            request_id = new_id("wrq")
            try:
                conn.execute(
                    text(
                        f"INSERT INTO {self._t(conn, 'access_request')} (id, organisation_id, purpose,"
                        " expected_volume, scope_requested, reduced, contact_name, contact_email, created_by,"
                        " tier_requested) VALUES (:id, :o, :purpose, :volume, CAST(:scope AS jsonb), :reduced,"
                        " :name, :email, :by, :plan)"
                    ),
                    {
                        "plan": plan,
                        "id": request_id,
                        "o": organisation_id,
                        "purpose": purpose,
                        "volume": volume,
                        "scope": json.dumps(scope),
                        "reduced": bool(request.get("reduced")),
                        "name": contact_name,
                        "email": contact_email,
                        "by": membership["id"],
                    },
                )
            except DBAPIError as error:
                if _sqlstate(error)[0] == "23505":
                    raise AccountError(
                        "request_open", 409, "This organisation already has a request open."
                    ) from None
                raise
            self._audit(
                conn,
                self._actor(conn, person_id),
                "access.requested",
                organisation=organisation_id,
                target=("access_request", request_id),
                summary={"reduced": bool(request.get("reduced")), "plan": plan},
                context=context,
            )
            return request_id

    def withdraw_request(self, person_id, organisation_id, request_id, context=None) -> None:
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "access.request")
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'access_request')} SET state = 'withdrawn', revision = revision + 1,"
                    " updated_at = statement_timestamp() WHERE id = :r AND organisation_id = :o"
                    " AND state IN ('submitted','needs_information')"
                ),
                {"r": request_id, "o": organisation_id},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No open request to withdraw.")
            self._audit(
                conn,
                self._actor(conn, person_id),
                "access.withdrawn",
                organisation=organisation_id,
                target=("access_request", request_id),
                context=context,
            )

    def end_access(self, person_id, organisation_id, context=None) -> None:
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "access.end")
            current = self.access(organisation_id, conn)
            if current["state"] != "active" or not current.get("entitlement"):
                raise AccountError(
                    "entitlement_inactive", 409, "There is no granted access to end; starter access stays."
                )
            self._change_entitlement(
                conn,
                current["entitlement"],
                "ended",
                self._actor(conn, person_id),
                "Ended by an owner",
                context,
            )

    def _change_entitlement(self, conn, entitlement_id, to_state, actor, reason, context) -> None:
        old = (
            conn.execute(
                text(
                    f"SELECT organisation_id, state FROM {self._t(conn, 'entitlement')} WHERE id = :e FOR UPDATE"
                ),
                {"e": entitlement_id},
            )
            .mappings()
            .one()
        )
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'entitlement')} SET state = :s, ends_at = CASE WHEN ends_at IS NULL"
                " OR ends_at > statement_timestamp() THEN statement_timestamp() ELSE ends_at END,"
                " revision = revision + 1, updated_at = statement_timestamp() WHERE id = :e"
            ),
            {"s": to_state, "e": entitlement_id},
        )
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'entitlement_event')} (entitlement_id, actor_type, actor_ref,"
                " from_state, to_state, reason) VALUES (:e, :t, :r, :f, :to, :reason)"
            ),
            {
                "e": entitlement_id,
                "t": actor[0],
                "r": actor[1],
                "f": old["state"],
                "to": to_state,
                "reason": reason,
            },
        )
        self._audit(
            conn,
            actor,
            "entitlement." + to_state,
            organisation=old["organisation_id"],
            target=("entitlement", entitlement_id),
            summary={"reason": reason},
            context=context,
        )

    # ---------- operator (MAC-11) ----------
    def grant_access(
        self,
        operator: str,
        organisation_id: str,
        *,
        tier: str,
        scope: dict,
        source: str,
        ends_at: datetime | None,
        reason: str,
        reference: str | None = None,
        request_id: str | None = None,
        context=None,
    ) -> str:
        """Grant (or change) an organisation's access from now; the current access ends in the same
        transaction, so the periods never overlap and nothing is reset (MAC-04)."""
        if source not in ("waiver", "manual") or not reason.strip():
            raise AccountError("invalid_input", 400, "A waiver or manual grant needs a reason.")
        actor = ("operator", operator)
        with self.engine.begin() as conn:
            found = conn.scalar(
                text(
                    f"SELECT id FROM {self._t(conn, 'organisation')} WHERE id = :o"
                    " AND state IN ('active','ownerless') FOR UPDATE"
                ),
                {"o": organisation_id},
            )
            if found is None:
                raise AccountError("not_found", 404, "No active organisation with that ID.")
            scope = self._plan_scope(conn, tier, scope, request=False)
            current = self.access(organisation_id, conn)
            if current["state"] == "active" and current.get("entitlement"):  # starter has nothing to end
                self._change_entitlement(
                    conn, current["entitlement"], "ended", actor, "Replaced by a new grant", context
                )
            entitlement_id = self._start_entitlement(
                conn, organisation_id, tier, source, scope, ends_at, reference, reason, actor
            )
            if request_id:
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'access_request')} SET state = 'granted', revision = revision + 1,"
                        " decision = CAST(:d AS jsonb), updated_at = statement_timestamp()"
                        " WHERE id = :r AND organisation_id = :o AND state IN ('submitted','needs_information')"
                    ),
                    {
                        "r": request_id,
                        "o": organisation_id,
                        "d": json.dumps(
                            {
                                "tier": tier,
                                "scope": scope,
                                "ends_at": ends_at and ends_at.isoformat(),
                                "reason": reason,
                            }
                        ),
                    },
                )
            self._audit(
                conn,
                actor,
                "entitlement.granted",
                organisation=organisation_id,
                target=("entitlement", entitlement_id),
                summary={"tier": tier, "scope": scope, "source": source},
                context=context,
            )
            return entitlement_id

    def _start_entitlement(
        self, conn, organisation_id, tier, source, scope, ends_at, reference, reason, actor
    ):
        entitlement_id = new_id("ent")
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'entitlement')} (id, organisation_id, tier_id, source, scope,"
                " starts_at, ends_at, state, reference, reason) VALUES (:id, :o, :tier, :source,"
                " CAST(:scope AS jsonb), statement_timestamp(), :ends, 'active', :ref, :reason)"
            ),
            {
                "id": entitlement_id,
                "o": organisation_id,
                "tier": tier,
                "source": source,
                "scope": json.dumps(scope),
                "ends": ends_at,
                "ref": reference,
                "reason": reason,
            },
        )
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'entitlement_event')} (entitlement_id, actor_type, actor_ref,"
                " to_state, reason) VALUES (:e, :t, :r, 'active', :reason)"
            ),
            {"e": entitlement_id, "t": actor[0], "r": actor[1], "reason": reason},
        )
        return entitlement_id

    def _plan_scope(self, conn, tier: str, scope: dict, request: bool = True) -> dict:
        """The scope a plan allows. A plan with a number of countries (Developer, Team) covers 1 to that
        many, chosen by the organisation; each country includes everything under it, and the
        continent's and regional bodies' law is within every plan (api.reach). A plan without a number
        covers every country: a request for it asks for all; an operator may still grant it narrower."""
        row = (
            conn.execute(
                text(
                    f"SELECT name, (allowance->>'jurisdictions')::int AS countries FROM {self._t(conn, 'tier')}"
                    " WHERE id = :t"
                ),
                {"t": tier},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AccountError("invalid_input", 400, "No such plan.")
        if row["countries"] is None:
            return {"all": True} if request else scope
        chosen = (
            scope.get("jurisdictions")
            if isinstance(scope, dict) and set(scope) == {"jurisdictions"}
            else None
        )
        if not (
            isinstance(chosen, list)
            and 1 <= len(chosen) <= row["countries"]
            and all(isinstance(c, str) and COUNTRY.match(c) for c in chosen)
            and len(set(chosen)) == len(chosen)
        ):
            raise AccountError(
                "invalid_input",
                400,
                f"{row['name']} covers up to {row['countries']} countries: choose 1 to {row['countries']}.",
            )
        return {"jurisdictions": sorted(chosen)}

    def _country_change_after(self, conn, organisation_id: str) -> datetime | None:
        """When an owner may next change the plan's countries; None when they may now."""
        return conn.scalar(
            text(
                f"SELECT max(at) + make_interval(days => {COUNTRY_CHANGE_DAYS}) FROM {self._t(conn, 'audit_event')}"
                " WHERE organisation_id = :o AND action = 'access.countries_changed'"
                f" HAVING max(at) + make_interval(days => {COUNTRY_CHANGE_DAYS}) > statement_timestamp()"
            ),
            {"o": organisation_id},
        )

    def change_countries(self, person_id: str, organisation_id: str, countries: list, context=None) -> None:
        """An owner swaps the plan's chosen countries, at most once every 30 days. The current access
        ends and the same plan continues with the new countries, so the history shows both."""
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "access.request")
            current = self.access(organisation_id, conn)
            if current["state"] != "active" or not current.get("entitlement"):
                raise AccountError("entitlement_inactive", 409, "Starter covers every country already.")
            old = (
                conn.execute(
                    text(
                        f"SELECT tier_id, source, scope, ends_at, reference FROM {self._t(conn, 'entitlement')}"
                        " WHERE id = :e"
                    ),
                    {"e": current["entitlement"]},
                )
                .mappings()
                .one()
            )
            if (
                conn.scalar(
                    text(f"SELECT allowance->>'jurisdictions' FROM {self._t(conn, 'tier')} WHERE id = :t"),
                    {"t": old["tier_id"]},
                )
                is None
            ):
                raise AccountError("invalid_input", 400, "Your plan covers every country.")
            scope = self._plan_scope(conn, old["tier_id"], {"jurisdictions": countries})
            if scope == old["scope"]:
                raise AccountError("invalid_input", 400, "Those are your countries already.")
            after = self._country_change_after(conn, organisation_id)
            if after is not None:
                raise AccountError(
                    "change_too_soon",
                    409,
                    f"You can change countries once a month: next from {after:%-d %B %Y}.",
                )
            actor = self._actor(conn, person_id)
            self._change_entitlement(
                conn, current["entitlement"], "ended", actor, "Countries changed", context
            )
            entitlement_id = self._start_entitlement(
                conn,
                organisation_id,
                old["tier_id"],
                old["source"],
                scope,
                old["ends_at"],
                old["reference"],
                "Countries changed by an owner",
                actor,
            )
            self._audit(
                conn,
                actor,
                "access.countries_changed",
                organisation=organisation_id,
                target=("entitlement", entitlement_id),
                summary={"from": old["scope"].get("jurisdictions"), "to": scope["jurisdictions"]},
                context=context,
            )

    def decide_request(self, operator, organisation_id, request_id, decision: str, reason: str, context=None):
        """Decline a request (with a reason; renewable after 30 days) or ask for more information."""
        if decision not in ("declined", "needs_information") or not reason.strip():
            raise AccountError("invalid_input", 400, "Decline or ask for information, with a reason.")
        with self.engine.begin() as conn:
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'access_request')} SET state = :s, revision = revision + 1,"
                    " decision = CAST(:d AS jsonb), updated_at = statement_timestamp(),"
                    " renewable_after = CASE WHEN :s = 'declined' THEN statement_timestamp() + interval '30 days'"
                    " END WHERE id = :r AND organisation_id = :o AND state IN ('submitted','needs_information')"
                ),
                {"s": decision, "d": json.dumps({"reason": reason}), "r": request_id, "o": organisation_id},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No open request with that ID.")
            self._audit(
                conn,
                ("operator", operator),
                "access." + decision,
                organisation=organisation_id,
                target=("access_request", request_id),
                context=context,
            )

    def revoke_access(self, operator, organisation_id, reason, context=None) -> None:
        """Stop an organisation's machine access. Starter access has no grant to revoke, so a revoked
        marker is recorded: the database's access check then gives it nothing until a new grant."""
        with self.engine.begin() as conn:
            current = self.access(organisation_id, conn)
            if current["state"] != "active":
                raise AccountError("entitlement_inactive", 409, "There is no active access to revoke.")
            if current.get("entitlement"):
                self._change_entitlement(
                    conn, current["entitlement"], "revoked", ("operator", operator), reason, context
                )
                return
            marker = new_id("ent")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'entitlement')} (id, organisation_id, tier_id, source, scope,"
                    " starts_at, ends_at, state, reason) VALUES (:id, :o, 'starter', 'manual', '{\"all\": true}',"
                    " statement_timestamp(), statement_timestamp() + interval '1 microsecond', 'revoked', :reason)"
                ),
                {"id": marker, "o": organisation_id, "reason": reason},
            )
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'entitlement_event')} (entitlement_id, actor_type, actor_ref,"
                    " to_state, reason) VALUES (:e, 'operator', :r, 'revoked', :reason)"
                ),
                {"e": marker, "r": operator, "reason": reason},
            )
            self._audit(
                conn,
                ("operator", operator),
                "entitlement.revoked",
                organisation=organisation_id,
                target=("entitlement", marker),
                summary={"reason": reason, "starter": True},
                context=context,
            )

    def open_requests(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT r.id, r.organisation_id, o.name AS organisation, o.kind, o.country, r.purpose,"
                    f" r.expected_volume, r.scope_requested, r.reduced, r.tier_requested, r.contact_name, r.contact_email, r.state,"
                    f" r.created_at FROM {self._t(conn, 'access_request')} r JOIN {self._t(conn, 'organisation')} o"
                    " ON o.id = r.organisation_id WHERE r.state IN ('submitted','needs_information')"
                    " ORDER BY r.created_at"
                )
            )
            return [dict(r) for r in rows.mappings()]

    def set_tiers(self, tiers: list[dict]) -> None:
        """Bring the tier catalogue in line with accounts/tiers.json (seed); tiers are never deleted."""
        with self.engine.begin() as conn:
            for tier in tiers:
                conn.execute(
                    text(
                        f"INSERT INTO {self._t(conn, 'tier')} (id, name, description, includes, allowance, published)"
                        " VALUES (:id, :name, :description, :includes, CAST(:allowance AS jsonb), :published)"
                        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,"
                        " includes = EXCLUDED.includes, allowance = EXCLUDED.allowance,"
                        f" published = EXCLUDED.published, revision = {self._t(conn, 'tier')}.revision + 1"
                        f" WHERE ({self._t(conn, 'tier')}.name, {self._t(conn, 'tier')}.description,"
                        f" {self._t(conn, 'tier')}.includes, {self._t(conn, 'tier')}.allowance,"
                        f" {self._t(conn, 'tier')}.published) IS DISTINCT FROM (EXCLUDED.name,"
                        " EXCLUDED.description, EXCLUDED.includes, EXCLUDED.allowance, EXCLUDED.published)"
                    ),
                    {
                        **{k: tier[k] for k in ("id", "name", "description", "published")},
                        "allowance": json.dumps(tier["allowance"]),
                        "includes": list(tier["includes"]),
                    },
                )

    def tiers(self, published_only=True) -> list[dict]:
        """The plans, in the order people compare them, with how each is shown (accounts/tiers.json)."""
        listings = {t["id"]: t.get("listing", {}) for t in json.loads(TIERS.read_text())}
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT id, name, description, includes, allowance FROM {self._t(conn, 'tier')}"
                    + (" WHERE published" if published_only else "")
                )
            )
            tiers = [dict(r) | {"listing": listings.get(r["id"], {})} for r in rows.mappings()]
        return sorted(tiers, key=lambda t: (t["listing"].get("order", 99), t["id"]))

    # ---------- API keys ----------
    def create_key(
        self,
        person_id: str,
        organisation_id: str,
        *,
        kind: str,
        name: str,
        days: int = keys.DEFAULT_DAYS,
        context=None,
    ) -> dict:
        """A new key, its secret returned once. Needs active access and a role that may hold it."""
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            return self._create_key(conn, person_id, organisation_id, membership, kind, name, days, context)

    def _create_key(
        self, conn, person_id, organisation_id, membership, kind, name, days, context, *, replacing=False
    ) -> dict:
        """``replacing``: a rotation's new key, which takes the old key's place and so is never refused
        by the cap (owner, 2026-10-21)."""
        name = " ".join((name or "").split())
        if kind not in ("personal", "service") or not 1 <= len(name) <= 80:
            raise AccountError("invalid_input", 400, "Give the key a name of 1 to 80 characters.")
        if not isinstance(days, int) or not 1 <= days <= keys.MAX_DAYS:
            raise AccountError("invalid_input", 400, "Keys last between 1 and 365 days.")
        self._require(membership, "key.personal" if kind == "personal" else "key.service")
        if self.access(organisation_id, conn)["state"] != "active":
            raise AccountError(
                "entitlement_inactive", 409, "Keys can be created only while the organisation has access."
            )
        limit = PERSONAL_KEYS_PER_MEMBER if kind == "personal" else SERVICE_KEYS_PER_ORGANISATION
        # A key that has been rotated and still works its overlap is outside the cap: its replacement
        # holds the place.
        if not replacing and limit <= conn.scalar(
            text(
                f"SELECT count(*) FROM {self._t(conn, 'api_key')} k WHERE k.organisation_id = :o AND k.kind = :k"
                " AND k.revoked_at IS NULL AND k.expires_at > statement_timestamp()"
                + (" AND k.holder_membership = :m" if kind == "personal" else "")
                + f" AND NOT EXISTS (SELECT 1 FROM {self._t(conn, 'api_key')} n"
                " WHERE n.organisation_id = k.organisation_id AND n.rotated_from = k.key_id)"
            ),
            {"o": organisation_id, "k": kind, "m": membership["id"]},
        ):
            raise AccountError("limit_reached", 409, f"You can have up to {limit} active keys of this kind.")
        new = keys.generate()
        actor = self._actor(conn, person_id)
        row = (
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'api_key')} (key_id, kind, organisation_id, holder_membership,"
                    " holder_person, created_by, name, secret_hash, last_four, expires_at) VALUES (:id, :kind, :o,"
                    " :m, :p, :by, :name, :hash, :four, statement_timestamp() + make_interval(days => :days))"
                    " RETURNING created_at, expires_at"
                ),
                {
                    "id": new.key_id,
                    "kind": kind,
                    "o": organisation_id,
                    "m": membership["id"] if kind == "personal" else None,
                    "p": person_id if kind == "personal" else None,
                    "by": actor[1],
                    "name": name,
                    "hash": keys.secret_hash(new.secret, self._pepper),
                    "four": new.last_four,
                    "days": days,
                },
            )
            .mappings()
            .one()
        )
        self._audit(
            conn,
            actor,
            "key.created",
            organisation=organisation_id,
            target=("key", new.public_id),
            summary={"kind": kind, "days": days},
            context=context,
        )
        return {"id": new.public_id, "kind": kind, "name": name, "secret": new.secret, **dict(row)}

    def list_keys(self, person_id: str, organisation_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            everyone = roles.can(membership["role"], "key.revoke_any")
            if not everyone and not roles.can(membership["role"], "key.personal"):
                return []
            rows = conn.execute(
                text(
                    f"SELECT 'key_' || k.key_id AS id, k.kind, k.name, k.last_four, k.created_at, k.expires_at,"
                    " k.last_used_at, k.revoked_at, k.revoked_reason, a.display_name AS created_by,"
                    " k.holder_membership = :m AS mine,"
                    f" EXISTS (SELECT 1 FROM {self._t(conn, 'api_key')} n WHERE n.organisation_id = k.organisation_id"
                    " AND n.rotated_from = k.key_id) AS replaced,"
                    " CASE WHEN k.revoked_at IS NOT NULL THEN 'revoked' WHEN k.expires_at <= statement_timestamp()"
                    f" THEN 'expired' ELSE 'active' END AS state FROM {self._t(conn, 'api_key')} k"
                    f" LEFT JOIN {self._t(conn, 'actor')} a ON a.id = k.created_by WHERE k.organisation_id = :o"
                    + ("" if everyone else " AND k.holder_membership = :m")
                    + " ORDER BY k.created_at DESC LIMIT 200"
                ),
                {"o": organisation_id, "m": membership["id"]},
            )
            return [dict(r) for r in rows.mappings()]

    def revoke_key(self, person_id: str, organisation_id: str, public_id: str, context=None) -> None:
        """Revoking only removes access, so it needs no fresh sign-in (MAC-03)."""
        key_id = public_id.removeprefix("key_")
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            any_key = roles.can(membership["role"], "key.revoke_any")
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'api_key')} SET revoked_at = statement_timestamp(),"
                    " revoked_reason = CASE WHEN holder_membership = :m THEN 'holder' ELSE 'administrator' END"
                    " WHERE key_id = :k AND organisation_id = :o AND revoked_at IS NULL"
                    + ("" if any_key else " AND holder_membership = :m")
                ),
                {"k": key_id, "o": organisation_id, "m": membership["id"]},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No such active key that you may revoke.")
            self._audit(
                conn,
                self._actor(conn, person_id),
                "key.revoked",
                organisation=organisation_id,
                target=("key", public_id),
                context=context,
            )

    def rotate_key(
        self, person_id: str, organisation_id: str, public_id: str, overlap_hours: int = 24, context=None
    ) -> dict:
        """A new key with the same name and kind, in the old one's place, so no cap ever refuses it (owner,
        2026-10-21). The old key keeps working for at most 24 hours outside the cap, or stops at once with
        no overlap (after a leak). A key has at most one predecessor still working: rotating it again
        during that overlap ends the predecessor now, and a key already replaced is not rotated again."""
        if not isinstance(overlap_hours, int) or not 0 <= overlap_hours <= 24:
            raise AccountError("invalid_input", 400, "The old key may keep working for 0 to 24 hours.")
        key_id = public_id.removeprefix("key_")
        with self.engine.begin() as conn:
            # The organisation's row is locked, so rotations and new keys in it take turns and each sees
            # the replacements made before it.
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            old = (
                conn.execute(
                    text(
                        f"SELECT kind, name, holder_membership, rotated_from, expires_at - created_at AS life,"
                        f" EXISTS (SELECT 1 FROM {self._t(conn, 'api_key')} n WHERE n.organisation_id = :o"
                        f" AND n.rotated_from = :k) AS replaced FROM {self._t(conn, 'api_key')}"
                        " WHERE key_id = :k AND organisation_id = :o AND revoked_at IS NULL"
                        " AND expires_at > statement_timestamp() FOR UPDATE"
                    ),
                    {"k": key_id, "o": organisation_id},
                )
                .mappings()
                .first()
            )
            mine = old is not None and old["holder_membership"] == membership["id"]
            if old is None or not (mine or roles.can(membership["role"], "key.revoke_any")):
                raise AccountError("not_found", 404, "No such active key that you may rotate.")
            if old["kind"] == "personal" and not mine:
                raise AccountError("role_forbidden", 403, "Only its holder can rotate a personal key.")
            if old["replaced"]:
                raise AccountError(
                    "already_rotated",
                    409,
                    "This key has already been replaced: rotate or revoke its replacement.",
                )
            new = self._create_key(
                conn,
                person_id,
                organisation_id,
                membership,
                old["kind"],
                old["name"],
                max(1, min(keys.MAX_DAYS, old["life"].days)),
                context,
                replacing=True,
            )
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'api_key')} SET revoked_at = statement_timestamp(),"
                    " revoked_reason = 'rotated' WHERE key_id = :k"
                    if overlap_hours == 0
                    else f"UPDATE {self._t(conn, 'api_key')} SET expires_at = least(expires_at,"
                    " statement_timestamp() + make_interval(hours => :h)) WHERE key_id = :k"
                ),
                {"k": key_id, "h": overlap_hours},
            )
            conn.execute(
                text(f"UPDATE {self._t(conn, 'api_key')} SET rotated_from = :old WHERE key_id = :new"),
                {"old": key_id, "new": new["id"].removeprefix("key_")},
            )
            # The key this one replaced, if still in its overlap, stops now: a key's place never holds
            # more than one key outside the cap.
            ended = old["rotated_from"] and conn.scalar(
                text(
                    f"UPDATE {self._t(conn, 'api_key')} SET revoked_at = statement_timestamp(),"
                    " revoked_reason = 'rotated' WHERE key_id = :p AND organisation_id = :o"
                    " AND revoked_at IS NULL AND expires_at > statement_timestamp() RETURNING 'key_' || key_id"
                ),
                {"p": old["rotated_from"], "o": organisation_id},
            )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "key.rotated",
                organisation=organisation_id,
                target=("key", public_id),
                summary={"replacement": new["id"], "overlap_hours": overlap_hours}
                | ({"ended_predecessor": ended} if ended else {}),
                context=context,
            )
            return new

    # ---------- connected apps ----------
    def verified_client(self, client_id: str) -> tuple[str, str] | None:
        """(name, publisher) of a tool pinned as a verified publisher, or None."""
        if not client_id.startswith("https://"):
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT name, publisher FROM {self._t(conn, 'verified_client')}"
                    " WHERE client_id = :c AND unpinned_at IS NULL"
                ),
                {"c": client_id},
            ).first()
        return (row.name, row.publisher) if row else None

    def create_grant(
        self,
        person_id: str,
        organisation_id: str,
        *,
        client_id: str,
        client_name: str,
        client_label: str,
        client_host: str | None,
        resource: str,
        context=None,
    ) -> str:
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "client.connect")
            if self.access(organisation_id, conn)["state"] != "active":
                raise AccountError(
                    "entitlement_inactive",
                    409,
                    "Apps can be connected only while the organisation has access.",
                )
            grant_id = new_id("grt")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'client_grant')} (id, organisation_id, person_id, membership_id,"
                    " client_id, client_name, client_label, client_host, resource)"
                    " VALUES (:id, :o, :p, :m, :cid, :name, :label, :host, :resource)"
                ),
                {
                    "id": grant_id,
                    "o": organisation_id,
                    "p": person_id,
                    "m": membership["id"],
                    "cid": client_id,
                    "name": client_name[:200],
                    "label": client_label,
                    "host": client_host,
                    "resource": resource,
                },
            )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "client.connected",
                organisation=organisation_id,
                target=("grant", grant_id),
                summary={"client": client_name[:200], "resource": resource},
                context=context,
            )
            return grant_id

    def list_grants(self, person_id: str, organisation_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            everyone = roles.can(membership["role"], "client.revoke_any")
            rows = conn.execute(
                text(
                    f"SELECT g.id, g.client_name, g.client_label, g.client_host, g.resource, g.created_at,"
                    " g.last_used_at, g.revoked_at, g.membership_id = :m AS mine, a.display_name AS person"
                    f" FROM {self._t(conn, 'client_grant')} g LEFT JOIN {self._t(conn, 'actor')} a"
                    " ON a.person_id = g.person_id WHERE g.organisation_id = :o"
                    + ("" if everyone else " AND g.membership_id = :m")
                    + " ORDER BY g.revoked_at IS NOT NULL, g.created_at DESC LIMIT 200"
                ),
                {"o": organisation_id, "m": membership["id"]},
            )
            return [dict(r) for r in rows.mappings()]

    def revoke_grant(self, person_id: str, organisation_id: str, grant_id: str, context=None) -> None:
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            any_grant = roles.can(membership["role"], "client.revoke_any")
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'client_grant')} SET revoked_at = statement_timestamp(),"
                    " revoked_reason = CASE WHEN membership_id = :m THEN 'person' ELSE 'administrator' END"
                    " WHERE id = :g AND organisation_id = :o AND revoked_at IS NULL"
                    + ("" if any_grant else " AND membership_id = :m")
                ),
                {"g": grant_id, "o": organisation_id, "m": membership["id"]},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No such connected app that you may disconnect.")
            self._audit(
                conn,
                self._actor(conn, person_id),
                "client.revoked",
                organisation=organisation_id,
                target=("grant", grant_id),
                context=context,
            )

    # ---------- members ----------
    def members(self, person_id: str, organisation_id: str) -> dict:
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            managers = roles.can(membership["role"], "audit.read")
            rows = (
                conn.execute(
                    text(
                        f"SELECT m.id, m.role, m.joined_at, p.display_name, p.second_factor,"
                        f" CASE WHEN :managers THEN p.email END AS email, m.person_id = :p AS you"
                        f" FROM {self._t(conn, 'membership')} m JOIN {self._t(conn, 'person')} p ON p.id = m.person_id"
                        " WHERE m.organisation_id = :o ORDER BY array_position(ARRAY['owner','admin','developer',"
                        "'viewer'], m.role), p.display_name"
                    ),
                    {"o": organisation_id, "p": person_id, "managers": managers},
                )
                .mappings()
                .all()
            )
            invitations = (
                conn.execute(
                    text(
                        f"SELECT id, email, role, created_at, expires_at FROM {self._t(conn, 'invitation')}"
                        " WHERE organisation_id = :o AND state = 'pending' AND expires_at > statement_timestamp()"
                        " ORDER BY created_at"
                    ),
                    {"o": organisation_id},
                )
                .mappings()
                .all()
                if managers
                else []
            )
            return {
                "members": [dict(r) for r in rows],
                "invitations": [dict(i) for i in invitations],
                "invite_roles": list(roles.invite_roles(membership["role"])),
            }

    def invite(self, person_id: str, organisation_id: str, email: str, role: str, context=None) -> str:
        email = canonical_email(email)
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            if role not in roles.invite_roles(membership["role"]):
                raise AccountError("role_forbidden", 403, "Your role cannot invite someone with that role.")
            counts = (
                conn.execute(
                    text(
                        f"SELECT (SELECT count(*) FROM {self._t(conn, 'membership')} WHERE organisation_id = :o) AS members,"
                        f" (SELECT count(*) FROM {self._t(conn, 'invitation')} WHERE organisation_id = :o"
                        " AND state = 'pending' AND expires_at > statement_timestamp()) AS pending,"
                        " member_limit, invitation_limit, EXISTS(SELECT 1 FROM"
                        f" {self._t(conn, 'membership')} m JOIN {self._t(conn, 'person')} p ON p.id = m.person_id"
                        " WHERE m.organisation_id = :o AND p.email = :e) AS member"
                        f" FROM {self._t(conn, 'organisation')} WHERE id = :o"
                    ),
                    {"o": organisation_id, "e": email},
                )
                .mappings()
                .one()
            )
            if counts["member"]:
                raise AccountError("already_member", 409, "That person is already a member.")
            if (
                counts["members"] + counts["pending"] >= counts["member_limit"]
                or counts["pending"] >= counts["invitation_limit"]
            ):
                raise AccountError(
                    "limit_reached", 409, "This organisation has reached its member or invitation limit."
                )
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'invitation')} SET state = 'expired', ended_at = statement_timestamp()"
                    " WHERE organisation_id = :o AND email = :e AND state = 'pending'"
                    " AND expires_at <= statement_timestamp()"
                ),
                {"o": organisation_id, "e": email},
            )
            invitation_id = new_id("inv")
            try:
                conn.execute(
                    text(
                        f"INSERT INTO {self._t(conn, 'invitation')} (id, organisation_id, email, role, invited_by)"
                        " VALUES (:id, :o, :e, :r, :by)"
                    ),
                    {
                        "id": invitation_id,
                        "o": organisation_id,
                        "e": email,
                        "r": role,
                        "by": membership["id"],
                    },
                )
            except DBAPIError as error:
                if _sqlstate(error)[0] == "23505":
                    raise AccountError(
                        "already_invited", 409, "That email already has a pending invitation."
                    ) from None
                raise
            self._audit(
                conn,
                self._actor(conn, person_id),
                "member.invited",
                organisation=organisation_id,
                target=("invitation", invitation_id),
                summary={"role": role},
                context=context,
            )
            return invitation_id

    def revoke_invitation(self, person_id, organisation_id, invitation_id, context=None) -> None:
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            self._require(membership, "member.invite")
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'invitation')} SET state = 'revoked', ended_at = statement_timestamp()"
                    " WHERE id = :i AND organisation_id = :o AND state = 'pending'"
                ),
                {"i": invitation_id, "o": organisation_id},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No such pending invitation.")
            self._audit(
                conn,
                self._actor(conn, person_id),
                "member.invitation_revoked",
                organisation=organisation_id,
                target=("invitation", invitation_id),
                context=context,
            )

    def invitations_for(self, person_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT i.id, i.role, i.expires_at, o.name AS organisation FROM {self._t(conn, 'invitation')} i"
                    f" JOIN {self._t(conn, 'organisation')} o ON o.id = i.organisation_id"
                    f" JOIN {self._t(conn, 'person')} p ON p.email = i.email WHERE p.id = :p"
                    " AND p.email_verified_at IS NOT NULL AND i.state = 'pending'"
                    " AND i.expires_at > statement_timestamp() AND o.state IN ('active','ownerless')"
                ),
                {"p": person_id},
            )
            return [dict(r) for r in rows.mappings()]

    def accept_invitation(self, person_id: str, invitation_id: str, context=None) -> str:
        """Accepted only by the person signed in with the invited, verified email."""
        with self.engine.begin() as conn:
            invitation = (
                conn.execute(
                    text(
                        f"SELECT i.id, i.organisation_id, i.role FROM {self._t(conn, 'invitation')} i"
                        f" JOIN {self._t(conn, 'person')} p ON p.email = i.email WHERE i.id = :i AND p.id = :p"
                        " AND p.email_verified_at IS NOT NULL AND i.state = 'pending'"
                        " AND i.expires_at > statement_timestamp() FOR UPDATE OF i"
                    ),
                    {"i": invitation_id, "p": person_id},
                )
                .mappings()
                .first()
            )
            if invitation is None:
                raise AccountError(
                    "invitation_invalid", 410, "This invitation has expired, was revoked or is not yours."
                )
            conn.execute(
                text(f"SELECT id FROM {self._t(conn, 'organisation')} WHERE id = :o FOR UPDATE"),
                {"o": invitation["organisation_id"]},
            )
            membership_id = new_id("mem")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'membership')} (id, organisation_id, person_id, role)"
                    " VALUES (:id, :o, :p, :r) ON CONFLICT (organisation_id, person_id) DO NOTHING"
                ),
                {
                    "id": membership_id,
                    "o": invitation["organisation_id"],
                    "p": person_id,
                    "r": invitation["role"],
                },
            )
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'invitation')} SET state = 'accepted', ended_at = statement_timestamp()"
                    " WHERE id = :i"
                ),
                {"i": invitation_id},
            )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "member.joined",
                organisation=invitation["organisation_id"],
                target=("membership", membership_id),
                summary={"role": invitation["role"]},
                context=context,
            )
            return invitation["organisation_id"]

    def change_role(self, person_id, organisation_id, membership_id, role, context=None) -> None:
        if role not in roles.ROLES:
            raise AccountError("invalid_input", 400, "Choose owner, admin, developer or viewer.")
        with self.engine.begin() as conn:
            actor = self._membership(conn, person_id, organisation_id, lock=True)
            target = (
                conn.execute(
                    text(
                        f"SELECT id, role FROM {self._t(conn, 'membership')} WHERE id = :m AND organisation_id = :o"
                    ),
                    {"m": membership_id, "o": organisation_id},
                )
                .mappings()
                .first()
            )
            if target is None:
                raise AccountError("not_found", 404, "No such member.")
            if not roles.may_change(actor["role"], target["id"] == actor["id"], target["role"], role):
                raise AccountError("role_forbidden", 403, "Your role cannot make that change.")
            self._db(
                conn,
                lambda: conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'membership')} SET role = :r, revision = revision + 1,"
                        " updated_at = statement_timestamp() WHERE id = :m"
                    ),
                    {"r": role, "m": membership_id},
                ),
            )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "member.role_changed",
                organisation=organisation_id,
                target=("membership", membership_id),
                summary={"from": target["role"], "to": role},
                context=context,
            )

    def remove_member(self, person_id, organisation_id, membership_id, context=None) -> None:
        """Removal, or leaving when the membership is the caller's own. Their personal keys and
        connected apps are revoked in the same transaction (database trigger)."""
        with self.engine.begin() as conn:
            actor = self._membership(conn, person_id, organisation_id, lock=True)
            target = (
                conn.execute(
                    text(
                        f"SELECT id, role FROM {self._t(conn, 'membership')} WHERE id = :m AND organisation_id = :o"
                    ),
                    {"m": membership_id, "o": organisation_id},
                )
                .mappings()
                .first()
            )
            if target is None:
                raise AccountError("not_found", 404, "No such member.")
            if not roles.may_remove(actor["role"], target["id"] == actor["id"], target["role"]):
                raise AccountError("role_forbidden", 403, "Your role cannot remove that member.")
            self._db(
                conn,
                lambda: conn.execute(
                    text(f"DELETE FROM {self._t(conn, 'membership')} WHERE id = :m"), {"m": membership_id}
                ),
            )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "member.left" if target["id"] == actor["id"] else "member.removed",
                organisation=organisation_id,
                target=("membership", membership_id),
                context=context,
            )

    def transfer_ownership(self, person_id, organisation_id, membership_id, keep_owner: bool, context=None):
        with self.engine.begin() as conn:
            actor = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(actor, "ownership.transfer")
            if membership_id == actor["id"]:
                raise AccountError("invalid_input", 400, "Choose another member.")
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'membership')} SET role = 'owner', revision = revision + 1,"
                    " updated_at = statement_timestamp() WHERE id = :m AND organisation_id = :o"
                ),
                {"m": membership_id, "o": organisation_id},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No such member.")
            if not keep_owner:
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'membership')} SET role = 'admin', revision = revision + 1,"
                        " updated_at = statement_timestamp() WHERE id = :m"
                    ),
                    {"m": actor["id"]},
                )
            self._audit(
                conn,
                self._actor(conn, person_id),
                "ownership.transferred",
                organisation=organisation_id,
                target=("membership", membership_id),
                summary={"kept_owner": keep_owner},
                context=context,
            )

    def delete_organisation(self, person_id, organisation_id, context=None) -> None:
        """Owner only: every key and app is revoked at once and the access ends (MAC-03)."""
        with self.engine.begin() as conn:
            membership = self._membership(conn, person_id, organisation_id, lock=True)
            self._require(membership, "organisation.delete")
            self._delete_organisation(conn, organisation_id, self._actor(conn, person_id), context)

    def _delete_organisation(self, conn, organisation_id, actor, context=None) -> None:
        """The deletion itself, in the caller's transaction (also used when an account closes)."""
        current = self.access(organisation_id, conn)
        if current["state"] == "active" and current.get("entitlement"):
            self._change_entitlement(
                conn, current["entitlement"], "ended", actor, "Organisation deleted", context
            )
        for table in ("api_key", "client_grant"):
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, table)} SET revoked_at = statement_timestamp(),"
                    " revoked_reason = 'organisation_deleted' WHERE organisation_id = :o AND revoked_at IS NULL"
                ),
                {"o": organisation_id},
            )
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'organisation')} SET state = 'deleted', deleted_at = statement_timestamp(),"
                " revision = revision + 1 WHERE id = :o"
            ),
            {"o": organisation_id},
        )
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'invitation')} SET state = 'void', ended_at = statement_timestamp()"
                " WHERE organisation_id = :o AND state = 'pending'"
            ),
            {"o": organisation_id},
        )
        conn.execute(
            text(f"DELETE FROM {self._t(conn, 'membership')} WHERE organisation_id = :o"),
            {"o": organisation_id},
        )
        self._audit(
            conn,
            actor,
            "organisation.deleted",
            organisation=organisation_id,
            target=("organisation", organisation_id),
            context=context,
        )

    # ---------- usage and audit ----------
    def usage(self, person_id: str, organisation_id: str, days: int = 30) -> list[dict]:
        with self.engine.connect() as conn:
            self._membership(conn, person_id, organisation_id)
            rows = conn.execute(
                text(
                    f"SELECT day, credential, class, requests, bytes, refusals FROM {self._t(conn, 'usage_day')}"
                    " WHERE organisation_id = :o AND day > (statement_timestamp() AT TIME ZONE 'UTC')::date - :d"
                    " ORDER BY day, credential, class"
                ),
                {"o": organisation_id, "d": max(1, min(days, 366))},
            )
            return [dict(r) for r in rows.mappings()]

    def audit_log(self, person_id: str, organisation_id: str, limit: int = 100, before: int | None = None):
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            self._require(membership, "audit.read")
            rows = conn.execute(
                text(
                    f"SELECT e.seq, e.id, e.at, e.actor_type, coalesce(a.display_name, CASE e.actor_type WHEN"
                    " 'operator' THEN 'Open Alvary' WHEN 'system' THEN 'Open Alvary' ELSE 'Former member' END)"
                    " AS actor, e.action, e.target_type, e.target_id, e.summary, e.network_prefix,"
                    f" e.client_family FROM {self._t(conn, 'audit_event')} e LEFT JOIN {self._t(conn, 'actor')} a"
                    " ON a.id = e.actor_ref AND e.actor_type = 'person' WHERE e.organisation_id = :o"
                    + (" AND e.seq < :before" if before else "")
                    + " ORDER BY e.seq DESC LIMIT :n"
                ),
                {"o": organisation_id, "before": before, "n": max(1, min(limit, 500))},
            )
            return [dict(r) for r in rows.mappings()]

    def _db(self, conn, statement):
        """Run a statement whose refusal the database decides (last owner), as an AccountError."""
        try:
            with conn.begin_nested():
                return statement()
        except DBAPIError as error:
            state, message = _sqlstate(error)
            if state == "P0001" and message == "last_owner":
                raise AccountError(
                    "last_owner",
                    409,
                    "An organisation needs an owner. Transfer ownership or delete it first.",
                ) from None
            raise


def valid_organisation(value: str) -> bool:
    return valid(value, "org")
