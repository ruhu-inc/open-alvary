"""Operator access actions (MAC-11), for the review workbench's Access section and the command line.

An operator answers access requests (grant, decline, ask for information), grants, changes and
revokes access, suspends and restores people and organisations, restores an owner to an
organisation that has none, revokes a key or a connected app by its ID (leak reports), and reads
usage and the whole audit log. Every change takes a reason, and where it rests on something checked
outside the service, an evidence reference; both go into the audit log under the operator's name.

Operators never see secrets (keys are stored only as keyed hashes, and the database role leaves
them out), cannot act as a person, and grant plans from the catalogue (the pilot plan included),
which reach only the published corpus. Who may act, and the fresh passkey check, are the workbench's.

  python -m accounts.access_admin provision-role ROLE   the database role the workbench signs in as
                                  (LOGIN with ALVARY_ACCESS_ROLE_PASSWORD set, else NOLOGIN)
"""

import argparse
import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from accounts import keys, mail, policies
from accounts.store import AccountError, Accounts, Context

STATES = {"suspend": "suspended", "restore": "active"}
KEY_ID = re.compile(r"^[0-9a-f]{32}$")
GRANT_ID = re.compile(r"^grt_[0-9A-HJKMNP-TV-Z]{26}$")


@dataclass(frozen=True)
class Action:
    """Who acted, why, on what evidence, and from where."""

    operator: str
    reason: str
    evidence: str | None = None
    context: Context | None = None

    def __post_init__(self):
        if not self.operator.strip():
            raise AccountError("invalid_input", 400, "Say who is acting.")
        if not self.reason.strip() or len(self.reason) > 500:
            raise AccountError("invalid_input", 400, "Give a reason (up to 500 characters).")
        if self.evidence is not None and len(self.evidence) > 500:
            raise AccountError("invalid_input", 400, "Keep the evidence reference under 500 characters.")

    def summary(self, **more) -> dict:
        return {
            "reason": self.reason.strip(),
            **({"evidence": self.evidence.strip()} if self.evidence else {}),
            **more,
        }


class AccessAdmin:
    def __init__(self, accounts: Accounts, mailer: mail.Mailer | None = None, site: str | None = None):
        self.accounts = accounts
        self.mailer = mailer
        self.site = (site or os.getenv("ALVARY_CONSOLE_BASE_URL", "https://open.alvary.ai")).rstrip("/")

    def _t(self, conn, name):
        return self.accounts._t(conn, name)

    def _rows(self, conn, sql, **params) -> list[dict]:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def _tell(self, organisation_id, request_id, build) -> None:
        if self.mailer is None:
            return
        notice = self.accounts.notice_recipients(organisation_id, request_id)
        for to in notice["to"]:
            self.mailer.send(build(to, notice["organisation"]))

    # --- reading ------------------------------------------------------------------------------

    def requests(self) -> list[dict]:
        return self.accounts.open_requests()

    def search(self, query: str) -> list[dict]:
        """Organisations by ID or name, and the organisations of a person found by email or ID."""
        query = (query or "").strip()
        if len(query) < 2:
            return []
        with self.accounts.engine.connect() as conn:
            return self._rows(
                conn,
                f"SELECT DISTINCT o.id, o.name, o.state, o.created_at FROM {self._t(conn, 'organisation')} o"
                f" LEFT JOIN {self._t(conn, 'membership')} m ON m.organisation_id = o.id"
                f" LEFT JOIN {self._t(conn, 'person')} p ON p.id = m.person_id"
                " WHERE o.state <> 'deleted' AND (o.id = :q OR o.name ILIKE :like OR lower(p.email) = lower(:q)"
                " OR p.id = :q) ORDER BY o.created_at DESC LIMIT 25",
                q=query,
                like="%" + query.replace("%", r"\%").replace("_", r"\_") + "%",
            )

    def organisation(self, organisation_id: str) -> dict:
        with self.accounts.engine.connect() as conn:
            found = self._rows(
                conn,
                f"SELECT id, name, kind, country, website, state, created_at, deleted_at"
                f" FROM {self._t(conn, 'organisation')} WHERE id = :o",
                o=organisation_id,
            )
            if not found:
                raise AccountError("not_found", 404, "No organisation with that ID.")
            entitlements = self._rows(
                conn,
                f"SELECT id, tier_id, source, scope, starts_at, ends_at, state, reference, reason"
                f" FROM {self._t(conn, 'entitlement')} WHERE organisation_id = :o ORDER BY starts_at DESC LIMIT 20",
                o=organisation_id,
            )
            members = self._rows(
                conn,
                f"SELECT m.id, m.role, m.joined_at, p.id AS person_id, p.email, p.display_name, p.state,"
                f" p.second_factor FROM {self._t(conn, 'membership')} m JOIN {self._t(conn, 'person')} p"
                " ON p.id = m.person_id WHERE m.organisation_id = :o ORDER BY m.joined_at",
                o=organisation_id,
            )
            api_keys = self._rows(
                conn,
                f"SELECT key_id, kind, name, last_four, holder_person, created_at, expires_at, last_used_at,"
                f" revoked_at, revoked_reason FROM {self._t(conn, 'api_key')} WHERE organisation_id = :o"
                " ORDER BY created_at DESC LIMIT 100",
                o=organisation_id,
            )
            apps = self._rows(
                conn,
                f"SELECT id, person_id, client_name, client_label, client_host, resource, created_at,"
                f" last_used_at, revoked_at, revoked_reason FROM {self._t(conn, 'client_grant')}"
                " WHERE organisation_id = :o ORDER BY created_at DESC LIMIT 100",
                o=organisation_id,
            )
            requests = self._rows(
                conn,
                f"SELECT id, purpose, expected_volume, scope_requested, reduced, tier_requested, contact_name,"
                f" contact_email, state, decision, created_at, updated_at FROM {self._t(conn, 'access_request')}"
                " WHERE organisation_id = :o ORDER BY created_at DESC LIMIT 20",
                o=organisation_id,
            )
            usage = self._rows(
                conn,
                f"SELECT day, class, sum(requests)::bigint AS requests, sum(bytes)::bigint AS bytes"
                f" FROM {self._t(conn, 'usage_day')} WHERE organisation_id = :o"
                " AND day > (statement_timestamp() AT TIME ZONE 'UTC')::date - 30 GROUP BY day, class ORDER BY day",
                o=organisation_id,
            )
        return {
            **found[0],
            "access": self.accounts.access(organisation_id),
            "entitlements": entitlements,
            "members": members,
            "keys": api_keys,
            "connected_apps": apps,
            "requests": requests,
            "usage": usage,
            "audit": self.audit(organisation_id=organisation_id, limit=50),
        }

    def audit(
        self,
        *,
        organisation_id: str | None = None,
        action: str | None = None,
        before: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """The whole log, newest first; people by their display name while their account exists."""
        where, params = [], {"n": max(1, min(limit, 500))}
        if organisation_id:
            where.append("e.organisation_id = :o")
            params["o"] = organisation_id
        if action:
            where.append("e.action LIKE :a")
            params["a"] = action.replace("%", "").replace("_", r"\_") + "%"
        if before:
            where.append("e.seq < :before")
            params["before"] = before
        with self.accounts.engine.connect() as conn:
            return self._rows(
                conn,
                f"SELECT e.seq, e.id, e.at, e.actor_type, CASE e.actor_type WHEN 'person' THEN"
                " coalesce(a.display_name, 'Former member') ELSE e.actor_ref END AS actor, e.organisation_id,"
                " e.action, e.target_type, e.target_id, e.summary, e.network_prefix, e.client_family,"
                f" e.request_id FROM {self._t(conn, 'audit_event')} e LEFT JOIN {self._t(conn, 'actor')} a"
                " ON a.id = e.actor_ref AND e.actor_type = 'person'"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY e.seq DESC LIMIT :n",
                **params,
            )

    # --- access decisions ---------------------------------------------------------------------

    def grant(
        self,
        action: Action,
        organisation_id: str,
        *,
        tier: str | None = None,
        jurisdictions: list[str] | None = None,
        until: datetime | None = None,
        waiver: bool = False,
        request_id: str | None = None,
    ) -> str:
        """Grant (or change) access from now; by default the plan and countries the request asked for."""
        asked = next((r for r in self.requests() if r["id"] == request_id), {}) if request_id else {}
        tier = tier or asked.get("tier_requested")
        if not tier:
            raise AccountError("invalid_input", 400, "Choose the plan (the request did not ask for one).")
        scope = (
            {"jurisdictions": [j.strip().lower() for j in jurisdictions if j.strip()]}
            if jurisdictions
            else asked.get("scope_requested") or {"all": True}
        )
        entitlement = self.accounts.grant_access(
            action.operator,
            organisation_id,
            tier=tier,
            scope=scope,
            source="waiver" if waiver else "manual",
            ends_at=until,
            reason=action.reason.strip(),
            reference=action.evidence,
            request_id=request_id,
            context=action.context,
        )
        ends = f"{until:%-d %B %Y}" if until else None
        self._tell(
            organisation_id, request_id, lambda to, name: mail.access_granted(to, name, ends, self.site)
        )
        return entitlement

    def decide(self, action: Action, organisation_id: str, request_id: str, decision: str) -> None:
        """Decline a request (renewable after 30 days) or ask for more information."""
        self.accounts.decide_request(
            action.operator, organisation_id, request_id, decision, action.reason.strip(), action.context
        )
        if decision == "declined":
            renewable = f"{datetime.now(UTC) + timedelta(days=30):%-d %B %Y}"
            self._tell(
                organisation_id,
                request_id,
                lambda to, name: mail.access_declined(to, name, action.reason, renewable, self.site),
            )
        else:
            self._tell(
                organisation_id,
                request_id,
                lambda to, name: mail.more_information(to, name, action.reason, self.site),
            )

    def revoke(self, action: Action, organisation_id: str) -> None:
        self.accounts.revoke_access(action.operator, organisation_id, action.reason.strip(), action.context)
        self._tell(organisation_id, None, lambda to, name: mail.access_revoked(to, name, self.site))

    # --- suspension and recovery --------------------------------------------------------------

    def set_organisation(self, action: Action, organisation_id: str, change: str) -> str:
        """Suspend an organisation (its keys and apps are refused at once) or restore it: active
        again, or ownerless when no active person owns it."""
        if change not in STATES:
            raise AccountError("invalid_input", 400, "Suspend or restore.")
        with self.accounts.engine.begin() as conn:
            org = self._t(conn, "organisation")
            state = conn.scalar(
                text(f"SELECT state FROM {org} WHERE id = :o FOR UPDATE"), {"o": organisation_id}
            )
            if state is None or state == "deleted":
                raise AccountError("not_found", 404, "No organisation with that ID.")
            if (change == "suspend") == (state == "suspended"):
                raise AccountError("conflict", 409, f"The organisation is {state} already.")
            target = (
                "suspended"
                if change == "suspend"
                else ("active" if self._owned(conn, organisation_id) else "ownerless")
            )
            conn.execute(
                text(
                    f"UPDATE {org} SET state = :s, revision = revision + 1, updated_at = statement_timestamp()"
                    " WHERE id = :o"
                ),
                {"s": target, "o": organisation_id},
            )
            self.accounts._audit(
                conn,
                ("operator", action.operator),
                f"organisation.{'suspended' if change == 'suspend' else 'restored'}",
                organisation=organisation_id,
                target=("organisation", organisation_id),
                summary=action.summary(state=target),
                context=action.context,
            )
            return target

    def set_person(self, action: Action, person_id: str, change: str) -> None:
        """Suspend a person (signed out everywhere; their keys and apps are refused) or restore them."""
        if change not in STATES:
            raise AccountError("invalid_input", 400, "Suspend or restore.")
        with self.accounts.engine.begin() as conn:
            people = self._t(conn, "person")
            state = conn.scalar(
                text(f"SELECT state FROM {people} WHERE id = :p FOR UPDATE"), {"p": person_id}
            )
            if state is None or state == "closed":
                raise AccountError("not_found", 404, "No open account with that ID.")
            if (change == "suspend") == (state == "suspended"):
                raise AccountError("conflict", 409, f"The account is {state} already.")
            conn.execute(
                text(
                    f"UPDATE {people} SET state = :s, revision = revision + 1, updated_at = statement_timestamp()"
                    " WHERE id = :p"
                ),
                {"s": STATES[change], "p": person_id},
            )
            if change == "suspend":
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'console_session')} SET revoked_at = statement_timestamp()"
                        " WHERE person_id = :p AND revoked_at IS NULL"
                    ),
                    {"p": person_id},
                )
            actor_ref = conn.scalar(
                text(f"SELECT id FROM {self._t(conn, 'actor')} WHERE person_id = :p"), {"p": person_id}
            )
            self.accounts._audit(
                conn,
                ("operator", action.operator),
                f"account.{'suspended' if change == 'suspend' else 'restored'}",
                target=("actor", actor_ref),
                summary=action.summary(),
                context=action.context,
            )

    def _owned(self, conn, organisation_id: str) -> bool:
        return bool(
            conn.scalar(
                text(
                    f"SELECT EXISTS(SELECT 1 FROM {self._t(conn, 'membership')} m JOIN {self._t(conn, 'person')} p"
                    " ON p.id = m.person_id WHERE m.organisation_id = :o AND m.role = 'owner' AND p.state = 'active')"
                ),
                {"o": organisation_id},
            )
        )

    def restore_owner(self, action: Action, organisation_id: str, membership_id: str) -> None:
        """Make a member the owner of an organisation that no active person owns, on evidence checked
        outside the service (for example the organisation's own confirmation)."""
        if not action.evidence:
            raise AccountError("invalid_input", 400, "Record the evidence you checked.")
        with self.accounts.engine.begin() as conn:
            org = self._t(conn, "organisation")
            state = conn.scalar(
                text(f"SELECT state FROM {org} WHERE id = :o FOR UPDATE"), {"o": organisation_id}
            )
            if state not in ("active", "ownerless"):
                raise AccountError("not_found", 404, "No active or ownerless organisation with that ID.")
            if self._owned(conn, organisation_id):
                raise AccountError(
                    "conflict", 409, "This organisation has an active owner, who can transfer it."
                )
            member = (
                conn.execute(
                    text(
                        f"SELECT m.role, p.state FROM {self._t(conn, 'membership')} m JOIN {self._t(conn, 'person')} p"
                        " ON p.id = m.person_id WHERE m.id = :m AND m.organisation_id = :o FOR UPDATE OF m"
                    ),
                    {"m": membership_id, "o": organisation_id},
                )
                .mappings()
                .first()
            )
            if member is None or member["state"] != "active":
                raise AccountError("not_found", 404, "No active member with that ID in this organisation.")
            conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'membership')} SET role = 'owner', revision = revision + 1,"
                    " updated_at = statement_timestamp() WHERE id = :m"
                ),
                {"m": membership_id},
            )
            conn.execute(
                text(
                    f"UPDATE {org} SET state = 'active', revision = revision + 1, updated_at = statement_timestamp()"
                    " WHERE id = :o AND state = 'ownerless'"
                ),
                {"o": organisation_id},
            )
            self.accounts._audit(
                conn,
                ("operator", action.operator),
                "ownership.restored",
                organisation=organisation_id,
                target=("membership", membership_id),
                summary=action.summary(previous_role=member["role"]),
                context=action.context,
            )

    def revoke_credential(self, action: Action, credential: str) -> dict:
        """Revoke a key (by its ID, or the leaked key itself, which is only parsed) or a connected app
        (by its grant ID), whichever organisation holds it."""
        credential = (credential or "").strip()
        key_id = keys.parse(credential) or (credential if KEY_ID.match(credential) else None)
        if key_id:
            table, column, kind, value = "api_key", "key_id", "key", key_id
        elif GRANT_ID.match(credential):
            table, column, kind, value = "client_grant", "id", "client", credential
        else:
            raise AccountError("invalid_input", 400, "Give a key, a key ID or a connected app's grant ID.")
        with self.accounts.engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, table)} SET revoked_at = statement_timestamp(), revoked_reason ="
                        f" 'operator' WHERE {column} = :v AND revoked_at IS NULL RETURNING organisation_id"
                    ),
                    {"v": value},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AccountError("not_found", 404, "No live key or connected app with that ID.")
            self.accounts._audit(
                conn,
                ("operator", action.operator),
                f"{kind}.revoked",
                organisation=row["organisation_id"],
                target=("api_key" if kind == "key" else "client_grant", value),
                summary=action.summary(by="operator"),
                context=action.context,
            )
            return {"kind": kind, "id": value, "organisation_id": row["organisation_id"]}

    # --- new terms (terms of service, "Changes") ------------------------------------------------

    def terms_notice(
        self, action: Action, version: str, effective: date, *, sooner: bool = False, dry_run: bool = False
    ) -> dict:
        """Email every owner of an organisation that a new version takes effect on `effective`, at least
        policies.NOTICE_DAYS ahead unless `sooner` (the law or the service's protection, said in the
        reason). Each owner told is in the audit log of each organisation they own, so running it again
        tells only those not yet told: new owners, or anyone whose mail failed."""
        changes = policies.CHANGES.get(version)
        if changes is None:
            raise AccountError("invalid_input", 400, f"accounts/policies.py has no CHANGES for {version}.")
        today = datetime.now(UTC).date()
        if effective < today:
            raise AccountError("invalid_input", 400, "The date the terms take effect has passed.")
        if effective < today + timedelta(days=policies.NOTICE_DAYS) and not sooner:
            raise AccountError(
                "invalid_input",
                400,
                f"New terms take effect at least {policies.NOTICE_DAYS} days after the notice. If the law or"
                " the service's protection needs it sooner, say why in the reason and add --sooner.",
            )
        with self.accounts.engine.connect() as conn:
            rows = self._rows(
                conn,
                f"SELECT p.id AS person_id, p.email, m.organisation_id FROM {self._t(conn, 'membership')} m"
                f" JOIN {self._t(conn, 'person')} p ON p.id = m.person_id"
                f" JOIN {self._t(conn, 'organisation')} o ON o.id = m.organisation_id"
                " WHERE m.role = 'owner' AND p.state = 'active' AND o.state IN ('active', 'suspended')"
                f" AND NOT EXISTS (SELECT 1 FROM {self._t(conn, 'audit_event')} e"
                " WHERE e.action = 'terms.notice_sent' AND e.organisation_id = m.organisation_id"
                " AND e.target_type = 'person' AND e.target_id = p.id AND e.summary->>'version' = :v)"
                " ORDER BY p.id, m.organisation_id",
                v=version,
            )
        owners: dict[str, dict] = {}
        for row in rows:
            owner = owners.setdefault(row["person_id"], {"email": row["email"], "organisations": []})
            owner["organisations"].append(row["organisation_id"])
        result = {
            "version": version,
            "effective": effective.isoformat(),
            "owners": len(owners),
            "organisations": len({row["organisation_id"] for row in rows}),
        }
        if dry_run or self.mailer is None:
            return result
        sent = 0
        for person_id, owner in owners.items():
            if not self.mailer.send(
                mail.terms_notice(owner["email"], version, effective, changes, self.site)
            ):
                continue  # logged by the mailer; the next run tries again
            sent += 1
            with self.accounts.engine.begin() as conn:
                for organisation_id in owner["organisations"]:
                    self.accounts._audit(
                        conn,
                        ("operator", action.operator),
                        "terms.notice_sent",
                        organisation=organisation_id,
                        target=("person", person_id),
                        summary=action.summary(version=version, effective=effective.isoformat()),
                        context=action.context,
                    )
        return {**result, "sent": sent, "failed": len(owners) - sent}

    # --- verified publishers (MAC-07) ---------------------------------------------------------

    def verified_clients(self) -> list[dict]:
        with self.accounts.engine.connect() as conn:
            return self._rows(
                conn,
                f"SELECT client_id, name, publisher, pinned_by, reason, pinned_at, unpinned_at, unpinned_by"
                f" FROM {self._t(conn, 'verified_client')} ORDER BY unpinned_at IS NOT NULL, publisher, name",
            )

    def pin(self, action: Action, client_id: str, name: str, publisher: str) -> None:
        """Show a tool as a verified publisher, after reviewing its client metadata document: the
        label applies to that exact address only, and to apps connected from now on."""
        client_id, name, publisher = client_id.strip(), " ".join(name.split()), " ".join(publisher.split())
        if not re.fullmatch(r"https://[^\s]{1,2000}", client_id) or not (
            0 < len(name) <= 100 and 0 < len(publisher) <= 100
        ):
            raise AccountError(
                "invalid_input", 400, "Give the https address of its client metadata, a name and a publisher."
            )
        with self.accounts.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'verified_client')} (client_id, name, publisher, pinned_by, reason)"
                    " VALUES (:c, :n, :p, :by, :r) ON CONFLICT (client_id) DO UPDATE SET name = EXCLUDED.name,"
                    " publisher = EXCLUDED.publisher, pinned_by = EXCLUDED.pinned_by, reason = EXCLUDED.reason,"
                    " pinned_at = statement_timestamp(), unpinned_at = NULL, unpinned_by = NULL"
                ),
                {
                    "c": client_id,
                    "n": name,
                    "p": publisher,
                    "by": action.operator,
                    "r": action.reason.strip(),
                },
            )
            self.accounts._audit(
                conn,
                ("operator", action.operator),
                "client.pinned",
                target=("client", client_id[:200]),
                summary=action.summary(name=name, publisher=publisher),
                context=action.context,
            )

    def unpin(self, action: Action, client_id: str) -> None:
        """Stop showing a tool as a verified publisher; apps already connected keep their label."""
        with self.accounts.engine.begin() as conn:
            done = conn.execute(
                text(
                    f"UPDATE {self._t(conn, 'verified_client')} SET unpinned_at = statement_timestamp(),"
                    " unpinned_by = :by WHERE client_id = :c AND unpinned_at IS NULL"
                ),
                {"c": client_id, "by": action.operator},
            ).rowcount
            if not done:
                raise AccountError("not_found", 404, "No pinned tool with that address.")
            self.accounts._audit(
                conn,
                ("operator", action.operator),
                "client.unpinned",
                target=("client", client_id[:200]),
                summary=action.summary(),
                context=action.context,
            )


# --- the database role ----------------------------------------------------------------------------

# What the workbench may do in the accounts schema, and nothing else: read what the Access section
# shows, write what its actions change. No secrets are readable: key hashes, session tokens and
# CSRF values are left out column by column.
READ = {
    "person": "id, revision, email, display_name, state, second_factor, created_at, updated_at, closed_at",
    "actor": "id, person_id, display_name",
    "organisation": None,
    "membership": None,
    "invitation": None,
    "tier": None,
    "entitlement": None,
    "entitlement_event": None,
    "access_request": None,
    "api_key": "key_id, kind, organisation_id, holder_membership, holder_person, created_by, name, last_four,"
    " created_at, expires_at, last_used_at, revoked_at, revoked_reason, rotated_from",
    "client_grant": "id, organisation_id, person_id, membership_id, client_id, client_name, client_label,"
    " client_host, resource, created_at, last_used_at, revoked_at, revoked_reason",
    "usage_day": None,
    "audit_event": None,
    "console_session": "person_id, revoked_at",
    "verified_client": None,
}
WRITE = (
    "INSERT ON {a}.audit_event, {a}.entitlement, {a}.entitlement_event",
    "UPDATE (state, ends_at, revision, updated_at) ON {a}.entitlement",
    "UPDATE (state, revision, decision, updated_at, renewable_after) ON {a}.access_request",
    "UPDATE (state, revision, updated_at) ON {a}.organisation, {a}.person",
    "UPDATE (role, revision, updated_at) ON {a}.membership",
    "UPDATE (revoked_at, revoked_reason) ON {a}.api_key, {a}.client_grant",
    "UPDATE (revoked_at) ON {a}.console_session",
    "INSERT ON {a}.verified_client",
    "UPDATE (name, publisher, pinned_by, reason, pinned_at, unpinned_at, unpinned_by) ON {a}.verified_client",
)


def grant_statements(accounts_schema: str, policy_schema: str, role: str) -> list[str]:
    a = accounts_schema
    statements = [f"GRANT USAGE ON SCHEMA {a}, {policy_schema} TO {role}"]
    for table, columns in READ.items():
        statements.append(f"GRANT SELECT{f' ({columns})' if columns else ''} ON {a}.{table} TO {role}")
    statements += [f"GRANT {w.format(a=a)} TO {role}" for w in WRITE]
    statements.append(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {a} TO {role}")
    statements.append(f"GRANT EXECUTE ON FUNCTION {policy_schema}.accounts_access(text) TO {role}")
    return statements


def provision_role(engine, role: str, password: str | None = None) -> list[str]:
    """Create the workbench's role and its grants; with a password it can sign in (LOGIN), without
    one it stays NOLOGIN. Run as an administrator with CREATEROLE (the deploy job's user)."""
    from rights.database import PolicyNamespace

    if password is not None and not re.fullmatch(r"[A-Za-z0-9]{32,128}", password):
        raise ValueError("The role's password must be 32 to 128 letters and digits")
    with engine.begin() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        quote = conn.dialect.identifier_preparer.quote_identifier
        login = f"LOGIN PASSWORD '{password}'" if password else "NOLOGIN"  # checked above: no quotes
        if conn.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :r)"), {"r": role}):
            if password:
                conn.execute(text(f"ALTER ROLE {quote(role)} {login}"))
        else:
            conn.execute(
                text(f"CREATE ROLE {quote(role)} {login} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
            )
        statements = grant_statements(quote(ns.schema("accounts")), quote(ns.schema("policy")), quote(role))
        for statement in statements:
            conn.execute(text(statement))
        return statements


def main(argv=None) -> int:
    from api.store import engine_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision-role").add_argument("role")
    args = parser.parse_args(argv)
    # The deploy job passes the password from Secret Manager; it is never printed.
    for statement in provision_role(
        engine_for(), args.role, os.environ.get("ALVARY_ACCESS_ROLE_PASSWORD") or None
    ):
        print(statement)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
