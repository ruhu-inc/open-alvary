"""A person's and an organisation's data: export, account deletion and retention (MAC-14).

Export: a person gets their own profile, sign-in methods (labels, never provider subjects),
policy acceptances, sessions, memberships, their personal keys' and connected apps' metadata, their
own audit entries, monitors and digest settings. An owner gets the organisation's records as the
console shows them to owners; never another person's private settings (their private monitors).

Deletion: credentials are revoked and memberships end at once; organisations where the person is
the only member are deleted with the account; a sole owner of an organisation with other members
must transfer or delete it first. Their monitors and email settings are deleted, sessions end, the
account is closed and its audit name no longer resolves (the entries stay, N07). The profile and
sign-in identities are purged 30 days later, together with the person's user at the identity
provider.

  python -m accounts.privacy purge     apply the retention schedule (run daily)
"""

import argparse
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
from sqlalchemy import text

from accounts.store import AccountError, Accounts

log = logging.getLogger("open_alvary.privacy")
EXPORT_SCHEMA = "account-export-1"
CLOSED_KEPT = timedelta(days=30)
LOCAL_ISSUERS = ("https://dev-login.invalid/",)  # api.console.DEV_ISSUER


def _rows(conn, sql, **params) -> list[dict]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def deletion_check(accounts: Accounts, person_id: str) -> dict:
    """What deleting this account would do: organisations it blocks on, deletes, or leaves."""
    with accounts.engine.connect() as conn:
        rows = _rows(
            conn,
            f"SELECT o.id, o.name, m.role,"
            f" (SELECT count(*) FROM {accounts._t(conn, 'membership')} x WHERE x.organisation_id = o.id) AS members,"
            f" (SELECT count(*) FROM {accounts._t(conn, 'membership')} x WHERE x.organisation_id = o.id"
            f" AND x.role = 'owner') AS owners FROM {accounts._t(conn, 'membership')} m"
            f" JOIN {accounts._t(conn, 'organisation')} o ON o.id = m.organisation_id"
            " WHERE m.person_id = :p AND o.state <> 'deleted' ORDER BY o.name",
            p=person_id,
        )
    plan = {"blocking": [], "deleted": [], "left": []}
    for row in rows:
        entry = {"id": row["id"], "name": row["name"], "role": row["role"], "members": row["members"]}
        if row["members"] == 1:
            plan["deleted"].append(entry)
        elif row["role"] == "owner" and row["owners"] == 1:
            plan["blocking"].append(entry)
        else:
            plan["left"].append(entry)
    return plan


def close_account(accounts: Accounts, person_id: str, context=None) -> dict:
    """Delete the account now (MAC-14): refused while it is the sole owner of a shared organisation."""
    plan = deletion_check(accounts, person_id)
    if plan["blocking"]:
        names = ", ".join(o["name"] for o in plan["blocking"])
        raise AccountError(
            "transfer_required",
            409,
            f"You are the only owner of {names}, which has other members. Make someone else an owner or"
            " delete it first.",
        )
    t = accounts._t
    with accounts.engine.begin() as conn:
        person = (
            conn.execute(
                text(f"SELECT email, state FROM {t(conn, 'person')} WHERE id = :p FOR UPDATE"),
                {"p": person_id},
            )
            .mappings()
            .first()
        )
        if person is None or person["state"] != "active":
            raise AccountError("account_inactive", 403, "This account is suspended or closed.")
        actor = accounts._actor(conn, person_id)
        for table, column in (("api_key", "holder_person"), ("client_grant", "person_id")):
            conn.execute(
                text(
                    f"UPDATE {t(conn, table)} SET revoked_at = statement_timestamp(), revoked_reason = 'account_closed'"
                    f" WHERE {column} = :p AND revoked_at IS NULL"
                ),
                {"p": person_id},
            )
        for organisation in plan["deleted"]:
            accounts._delete_organisation(conn, organisation["id"], actor, context)
        for organisation in plan["left"]:
            conn.execute(
                text(f"DELETE FROM {t(conn, 'membership')} WHERE organisation_id = :o AND person_id = :p"),
                {"o": organisation["id"], "p": person_id},
            )
            accounts._audit(
                conn,
                actor,
                "member.left",
                organisation=organisation["id"],
                summary={"account_closed": True},
                context=context,
            )
        # Their monitors (with their filters and matches) and email settings go now.
        for table, column in (
            ("watch", "owner_person_id"),
            ("watch_member", "person_id"),
            ("inbox_read", "person_id"),
            ("notification_pref", "person_id"),
            ("unsubscribe_token", "person_id"),
        ):
            conn.execute(text(f"DELETE FROM {t(conn, table)} WHERE {column} = :p"), {"p": person_id})
        conn.execute(
            text(
                f"UPDATE {t(conn, 'console_session')} SET revoked_at = statement_timestamp()"
                " WHERE person_id = :p AND revoked_at IS NULL"
            ),
            {"p": person_id},
        )
        conn.execute(
            text(
                f"UPDATE {t(conn, 'person')} SET state = 'closed', closed_at = statement_timestamp(),"
                " revision = revision + 1 WHERE id = :p"
            ),
            {"p": person_id},
        )
        accounts._audit(conn, actor, "account.closed", target=("person", person_id), context=context)
        # The audit entries stay; their actor reference no longer resolves to a person (N07).
        conn.execute(text(f"DELETE FROM {t(conn, 'actor')} WHERE person_id = :p"), {"p": person_id})
    return {"email": person["email"], "deleted_organisations": plan["deleted"], "left": plan["left"]}


# ---------- exports ----------
def _stamp(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _clean(rows):
    return [{k: _stamp(v) for k, v in row.items()} for row in rows]


def export_person(accounts: Accounts, person_id: str) -> dict:
    t = accounts._t
    with accounts.engine.connect() as conn:
        person = _rows(
            conn,
            f"SELECT id, email, email_verified_at, display_name, state, second_factor, created_at, updated_at"
            f" FROM {t(conn, 'person')} WHERE id = :p",
            p=person_id,
        )
        actor = conn.scalar(text(f"SELECT id FROM {t(conn, 'actor')} WHERE person_id = :p"), {"p": person_id})
        memberships = _rows(
            conn,
            f"SELECT o.id AS organisation_id, o.name AS organisation, m.role, m.joined_at"
            f" FROM {t(conn, 'membership')} m JOIN {t(conn, 'organisation')} o ON o.id = m.organisation_id"
            " WHERE m.person_id = :p ORDER BY m.joined_at",
            p=person_id,
        )
        data = {
            "schema": EXPORT_SCHEMA,
            "generated_at": datetime.now(UTC).isoformat(),
            "person": _clean(person)[0] if person else None,
            "sign_in_methods": _clean(
                _rows(
                    conn,
                    f"SELECT method, label, linked_at, last_used_at FROM {t(conn, 'identity')} WHERE person_id = :p",
                    p=person_id,
                )
            ),
            "policy_acceptances": _clean(
                _rows(
                    conn,
                    f"SELECT document, version, accepted_at FROM {t(conn, 'policy_acceptance')} WHERE person_id = :p"
                    " ORDER BY accepted_at",
                    p=person_id,
                )
            ),
            "sessions": _clean(
                _rows(
                    conn,
                    f"SELECT client_family, network_prefix, created_at, last_seen_at, expires_at, revoked_at"
                    f" FROM {t(conn, 'console_session')} WHERE person_id = :p ORDER BY created_at",
                    p=person_id,
                )
            ),
            "memberships": _clean(memberships),
            "personal_keys": _clean(
                _rows(
                    conn,
                    f"SELECT 'key_' || key_id AS id, organisation_id, kind, name, last_four, created_at, expires_at,"
                    f" last_used_at, revoked_at, revoked_reason FROM {t(conn, 'api_key')} WHERE holder_person = :p",
                    p=person_id,
                )
            ),
            "connected_apps": _clean(
                _rows(
                    conn,
                    f"SELECT id, organisation_id, client_name, client_label, client_host, resource, created_at,"
                    f" last_used_at, revoked_at FROM {t(conn, 'client_grant')} WHERE person_id = :p",
                    p=person_id,
                )
            ),
            "audit_entries": _clean(
                _rows(
                    conn,
                    f"SELECT id, at, organisation_id, action, target_type, target_id, summary, network_prefix,"
                    f" client_family FROM {t(conn, 'audit_event')} WHERE actor_type = 'person' AND actor_ref = :a"
                    " ORDER BY seq",
                    a=actor or "",
                )
            ),
            "monitors": _clean(
                _rows(
                    conn,
                    f"SELECT id, organisation_id, name, filters, state, visibility, created_at, paused_at,"
                    f" (SELECT count(*) FROM {t(conn, 'inbox_item')} i WHERE i.watch_id = w.id) AS matches"
                    f" FROM {t(conn, 'watch')} w WHERE owner_person_id = :p ORDER BY created_at",
                    p=person_id,
                )
            ),
            "digest_settings": _clean(
                _rows(
                    conn,
                    f"SELECT timezone, daily_minute, weekly_day, email_on, bounced_at FROM {t(conn, 'notification_pref')}"
                    " WHERE person_id = :p",
                    p=person_id,
                )
            ),
            "digests_sent": _clean(
                _rows(
                    conn,
                    f"SELECT cadence, window_start, window_end, event_count, outcome FROM {t(conn, 'digest')}"
                    " WHERE person_id = :p ORDER BY window_start",
                    p=person_id,
                )
            ),
            "webhook_endpoints_created": _clean(
                _rows(
                    conn,
                    f"SELECT id, organisation_id, url, description, state, created_at FROM {t(conn, 'webhook_endpoint')}"
                    " WHERE created_by = :p",
                    p=person_id,
                )
            ),
        }
    return data


def export_organisation(accounts: Accounts, person_id: str, organisation_id: str) -> dict:
    """Owners only: the organisation's records as the console shows them to owners."""
    t = accounts._t
    with accounts.engine.connect() as conn:
        membership = accounts._membership(conn, person_id, organisation_id)
        if membership["role"] != "owner":
            raise AccountError("role_forbidden", 403, "Only owners export an organisation's records.")
        shared = _rows(
            conn,
            f"SELECT id, name, filters, state, created_at FROM {t(conn, 'watch')} WHERE organisation_id = :o"
            " AND visibility = 'organisation' ORDER BY created_at",
            o=organisation_id,
        )
        audit = _rows(
            conn,
            f"SELECT e.id, e.at, e.actor_type, coalesce(a.display_name, CASE WHEN e.actor_type = 'person'"
            f" THEN 'Deleted account' ELSE e.actor_ref END) AS actor, e.action, e.target_type, e.target_id,"
            f" e.summary, e.network_prefix, e.client_family FROM {t(conn, 'audit_event')} e"
            f" LEFT JOIN {t(conn, 'actor')} a ON e.actor_type = 'person' AND a.id = e.actor_ref"
            " WHERE e.organisation_id = :o ORDER BY e.seq",
            o=organisation_id,
        )
        endpoints = _rows(
            conn,
            f"SELECT id, url, description, state, created_at, verified_at FROM {t(conn, 'webhook_endpoint')}"
            " WHERE organisation_id = :o ORDER BY created_at",
            o=organisation_id,
        )
    return json.loads(
        json.dumps(
            {
                "schema": EXPORT_SCHEMA,
                "generated_at": datetime.now(UTC).isoformat(),
                "organisation": accounts.organisation(person_id, organisation_id),
                "members": accounts.members(person_id, organisation_id),
                "access": accounts.access_details(person_id, organisation_id),
                "keys": accounts.list_keys(person_id, organisation_id),
                "connected_apps": accounts.list_grants(person_id, organisation_id),
                "usage": accounts.usage(person_id, organisation_id, days=730),
                "audit_log": _clean(audit),
                "shared_monitors": _clean(shared),
                "webhook_endpoints": _clean(endpoints),
            },
            default=_stamp,
        )
    )


# ---------- the identity provider ----------
class ProviderDeleter:
    """Deletes a person's user at Auth0 with a client allowed only delete:users
    (deployment/auth0/configure.py creates it)."""

    def __init__(self, issuer: str, client_id: str, client_secret: str, http: httpx.Client | None = None):
        self.issuer = issuer.rstrip("/") + "/"
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=20)
        self._token = None

    @classmethod
    def from_environment(cls) -> "ProviderDeleter | None":
        issuer, client = os.getenv("ALVARY_CONSOLE_ISSUER"), os.getenv("ALVARY_AUTH0_DELETION_CLIENT_ID")
        secret = os.getenv("ALVARY_AUTH0_DELETION_CLIENT_SECRET")
        return cls(issuer, client, secret) if issuer and client and secret else None

    def delete(self, subject: str) -> bool:
        if self._token is None:
            answer = self.http.post(
                self.issuer + "oauth/token",
                json={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "audience": self.issuer + "api/v2/",
                },
            )
            answer.raise_for_status()
            self._token = answer.json()["access_token"]
        answer = self.http.delete(
            self.issuer + "api/v2/users/" + quote(subject, safe=""),
            headers={"Authorization": f"Bearer {self._token}"},
        )
        return answer.status_code in (204, 404)  # already gone counts


# ---------- retention ----------
def purge(accounts: Accounts, deleter: ProviderDeleter | None = None, now: datetime | None = None) -> dict:
    """Apply the retention schedule of the privacy notice. A closed account is purged 30 days after
    closing, and only once its provider user is deleted (so the link to delete it is never lost)."""
    now = now or datetime.now(UTC)
    t = accounts._t
    done = {"accounts": 0, "provider_pending": 0}
    with accounts.engine.connect() as conn:
        closed = _rows(
            conn,
            f"SELECT p.id, array_agg(i.issuer || ' ' || i.subject) FILTER (WHERE i.id IS NOT NULL) AS identities"
            f" FROM {t(conn, 'person')} p LEFT JOIN {t(conn, 'identity')} i ON i.person_id = p.id"
            " WHERE p.state = 'closed' AND p.closed_at < :cut GROUP BY p.id",
            cut=now - CLOSED_KEPT,
        )
    for person in closed:
        # Every identity at a real provider must be deleted there first; local trial identities
        # (the console's dev login) exist nowhere else.
        provider = [s.split(" ", 1) for s in person["identities"] or [] if not s.startswith(LOCAL_ISSUERS)]
        if provider and (deleter is None or any(i.rstrip("/") + "/" != deleter.issuer for i, _ in provider)):
            done["provider_pending"] += 1
            continue
        try:
            if not all(deleter.delete(subject) for _, subject in provider):
                done["provider_pending"] += 1
                continue
        except httpx.HTTPError:
            log.warning("provider deletion failed; retried on the next run")
            done["provider_pending"] += 1
            continue
        with accounts.engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {t(conn, 'person')} WHERE id = :p"), {"p": person["id"]})
        done["accounts"] += 1
    with accounts.engine.begin() as conn:
        for name, table, condition, params in (
            (
                "sessions",
                "console_session",
                "greatest(expires_at, coalesce(revoked_at, expires_at)) < :d30",
                {},
            ),
            ("sign_in_flows", "sign_in_flow", "expires_at < :now", {}),
            ("invitations", "invitation", "coalesce(ended_at, expires_at) < :d30", {}),
            ("keys", "api_key", "revoked_at < :y1", {}),
            ("connected_apps", "client_grant", "revoked_at < :y1", {}),
            ("usage_days", "usage_day", "day < CAST(:y2 AS date)", {}),
        ):
            done[name] = conn.execute(
                text(f"DELETE FROM {t(conn, table)} WHERE {condition}"),
                {
                    "now": now,
                    "d30": now - CLOSED_KEPT,
                    "y1": now - timedelta(days=365),
                    "y2": now - timedelta(days=730),
                }
                | params,
            ).rowcount
        # The audit log keeps two years; only this purge may remove entries, and only older ones.
        conn.execute(text("SELECT set_config('open_alvary.retention_purge', 'on', true)"))
        done["audit_entries"] = conn.execute(
            text(f"DELETE FROM {t(conn, 'audit_event')} WHERE at < :y2"), {"y2": now - timedelta(days=730)}
        ).rowcount
    return done


def main(argv=None) -> int:
    from api.store import engine_for
    from changes.worker import accounts_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["purge"])
    parser.parse_args(argv)
    print(json.dumps(purge(accounts_for(engine_for()), ProviderDeleter.from_environment())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
