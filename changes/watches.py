"""Watches, their inbox and email preferences (SUB C2, changes-contracts.md).

A watch belongs to an organisation and the person who made it. Shared watches (plans that allow
them) are visible to every member; only the owner edits one, and each member chooses their own
email. Filters are the change feed's own; a watch matches only changes committed after it starts.
Plan limits come from the organisation's tier allowance. Filters never enter logs or the audit
trail: they can reveal what a client is working on.
"""

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from accounts.ids import new_id, valid
from accounts.store import AccountError, Accounts
from changes.feed import Feed, FeedError, Filters, _Visibility

MATCHER = "lexical-1"  # pinned matching rules; a change needs a visible new revision (SUB-03)
EMAIL = ("off", "daily", "weekly")
DIMENSIONS = ("jurisdiction", "work", "type", "class", "language")
TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def plan_limits(access: dict) -> dict:
    """What the organisation's plan allows (D19 proposals in accounts/tiers.json)."""
    if access.get("state") != "active":
        return {"watches": 0, "digest": "off", "shared": False}
    allowance = (access.get("tier") or {}).get("allowance") or {}
    return {
        "watches": int(allowance.get("watches", 3)),
        "digest": allowance.get("digest", "weekly"),
        "shared": bool(allowance.get("shared_watches", False)),
    }


def parse_filters(raw) -> Filters:
    """A watch's filters: the feed's filter object, with at least one dimension."""
    if not isinstance(raw, dict) or not set(raw) <= {*DIMENSIONS, "q"}:
        raise AccountError("invalid_input", 400, "Choose what to monitor.")
    values = {}
    for name in DIMENSIONS:
        given = raw.get(name) or []
        if not isinstance(given, list) or not all(isinstance(v, str) for v in given):
            raise AccountError("invalid_input", 400, "Choose what to monitor.")
        values[name] = given
    if raw.get("q"):
        if not isinstance(raw["q"], str):
            raise AccountError("invalid_input", 400, "The keyword must be text.")
        values["q"] = [raw["q"]]
    try:
        filters = Filters.parse(values)
    except FeedError as error:
        raise AccountError(error.code, 400, error.detail) from None
    if not any((filters.jurisdiction, filters.work, filters.type, filters.cls, filters.language, filters.q)):
        raise AccountError("invalid_input", 400, "Choose at least one country, record, type or keyword.")
    return filters


def stored_filters(canonical: dict) -> Filters:
    values = {k: v for k, v in canonical.items() if k != "q" and v}
    if canonical.get("q"):
        values["q"] = [canonical["q"]]
    return Filters.parse(values)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Watches:
    def __init__(self, accounts: Accounts):
        self.accounts, self.engine = accounts, accounts.engine

    def _t(self, conn, name):
        return self.accounts._t(conn, name)

    # ---------- reading ----------
    def list(self, person_id: str, organisation_id: str) -> dict:
        with self.engine.connect() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            rows = conn.execute(
                text(
                    f"SELECT w.*, m.email AS my_email, (w.owner_person_id = :p) AS mine,"
                    f" (SELECT count(*) FROM {self._t(conn, 'inbox_item')} i WHERE i.watch_id = w.id AND NOT EXISTS"
                    f" (SELECT 1 FROM {self._t(conn, 'inbox_read')} r WHERE r.inbox_item_id = i.id AND r.person_id = :p))"
                    f" AS unread FROM {self._t(conn, 'watch')} w LEFT JOIN {self._t(conn, 'watch_member')} m"
                    " ON m.watch_id = w.id AND m.person_id = :p WHERE w.organisation_id = :o"
                    " AND (w.owner_person_id = :p OR w.visibility = 'organisation') ORDER BY w.created_at"
                ),
                {"p": person_id, "o": organisation_id},
            ).mappings()
            watches = [self._view(dict(r)) for r in rows]
            total = conn.scalar(
                text(f"SELECT count(*) FROM {self._t(conn, 'watch')} WHERE organisation_id = :o"),
                {"o": organisation_id},
            )
            limits = plan_limits(self.accounts.access(organisation_id, conn))
            return {"watches": watches, "limits": limits | {"used": total}}

    @staticmethod
    def _view(row: dict) -> dict:
        return {
            "id": row["id"],
            "revision": row["revision"],
            "name": row["name"],
            "filters": row["filters"],
            "state": row["state"],
            "visibility": row["visibility"],
            "mine": row["mine"],
            "email": row.get("my_email") or "off",
            "unread": row.get("unread", 0),
            "matcher_revision": row["matcher_revision"],
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "paused_at": row["paused_at"],
        }

    def _watch(self, conn, person_id, organisation_id, watch_id, *, owner=False, lock=False):
        if not valid(watch_id, "wat"):
            raise AccountError("not_found", 404, "No such monitor.")
        row = (
            conn.execute(
                text(
                    f"SELECT w.*, (w.owner_person_id = :p) AS mine FROM {self._t(conn, 'watch')} w WHERE w.id = :w"
                    " AND w.organisation_id = :o AND (w.owner_person_id = :p OR w.visibility = 'organisation')"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"w": watch_id, "o": organisation_id, "p": person_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AccountError("not_found", 404, "No such monitor.")
        if owner and not row["mine"]:
            raise AccountError("role_forbidden", 403, "Only the person who made this monitor can change it.")
        return dict(row)

    # ---------- writing ----------
    def create(self, person_id: str, organisation_id: str, data: dict, context=None) -> dict:
        name = " ".join(str(data.get("name") or "").split())
        if not 1 <= len(name) <= 100:
            raise AccountError("invalid_input", 400, "Give the monitor a name of 1 to 100 characters.")
        filters = parse_filters(data.get("filters"))
        email = data.get("email", "off")
        visibility = data.get("visibility", "private")
        with self.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id, lock=True)
            limits = plan_limits(self.accounts.access(organisation_id, conn))
            self._check_channels(limits, email, visibility)
            used = conn.scalar(
                text(f"SELECT count(*) FROM {self._t(conn, 'watch')} WHERE organisation_id = :o"),
                {"o": organisation_id},
            )
            if used >= limits["watches"]:
                raise AccountError(
                    "limit_reached",
                    409,
                    f"Your plan includes {limits['watches']} monitors. See Plan and limits for more.",
                )
            feed = self._feed(conn)
            self._validate(feed, filters)
            watch_id = new_id("wat")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'watch')} (id, organisation_id, owner_person_id, name, filters,"
                    " filter_hash, matcher_revision, visibility, start_epoch, start_revision) VALUES (:id, :o, :p,"
                    " :name, CAST(:filters AS jsonb), :hash, :matcher, :visibility, :epoch, :start)"
                ),
                {
                    "id": watch_id,
                    "o": organisation_id,
                    "p": person_id,
                    "name": name,
                    "filters": json.dumps(filters.canonical()),
                    "hash": filters.hash(),
                    "matcher": MATCHER,
                    "visibility": visibility,
                    "epoch": feed.state()["epoch"],
                    "start": feed.high(),
                },
            )
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'watch_member')} (watch_id, person_id, email) VALUES (:w, :p, :e)"
                ),
                {"w": watch_id, "p": person_id, "e": email},
            )
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "watch.created",
                organisation=organisation_id,
                target=("watch", watch_id),
                context=context,
            )
            return self._view(
                self._watch(conn, person_id, organisation_id, watch_id) | {"my_email": email, "unread": 0}
            )

    def update(self, person_id: str, organisation_id: str, watch_id: str, data: dict, context=None) -> dict:
        with self.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            row = self._watch(conn, person_id, organisation_id, watch_id, owner=True, lock=True)
            if data.get("revision") is not None and data["revision"] != row["revision"]:
                raise AccountError(
                    "revision_conflict", 409, "This monitor changed meanwhile. Reload and try again."
                )
            limits = plan_limits(self.accounts.access(organisation_id, conn))
            changes: dict = {}
            if "name" in data:
                name = " ".join(str(data["name"] or "").split())
                if not 1 <= len(name) <= 100:
                    raise AccountError(
                        "invalid_input", 400, "Give the monitor a name of 1 to 100 characters."
                    )
                changes["name"] = name
            feed = self._feed(conn)
            if "filters" in data:
                filters = parse_filters(data["filters"])
                self._validate(feed, filters)
                if filters.hash() != row["filter_hash"]:
                    # New filters, new boundary: nothing earlier is rematched (SUB-03).
                    changes |= {
                        "filters": json.dumps(filters.canonical()),
                        "filter_hash": filters.hash(),
                        "start_epoch": feed.state()["epoch"],
                        "start_revision": feed.high(),
                    }
            if "visibility" in data:
                self._check_channels(limits, "off", data["visibility"])
                changes["visibility"] = data["visibility"]
            if "state" in data:
                if data["state"] not in ("active", "paused"):
                    raise AccountError("invalid_input", 400, "A monitor is active or paused.")
                if data["state"] != row["state"]:
                    changes["state"] = data["state"]
                    if data["state"] == "paused":
                        changes["paused_at"] = datetime.now(UTC)
                    else:
                        # Resuming continues from now: paused intervals are not caught up (SUB-03).
                        changes |= {
                            "paused_at": None,
                            "activated_at": datetime.now(UTC),
                            "start_epoch": feed.state()["epoch"],
                            "start_revision": feed.high(),
                        }
            if "email" in data:
                self._set_email(conn, limits, row, person_id, data["email"])
            if changes:
                assignments = ", ".join(
                    f"{k} = CAST(:{k} AS jsonb)" if k == "filters" else f"{k} = :{k}" for k in changes
                )
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'watch')} SET {assignments}, revision = revision + 1,"
                        " updated_at = statement_timestamp() WHERE id = :id"
                    ),
                    changes | {"id": watch_id},
                )
                self.accounts._audit(
                    conn,
                    self.accounts._actor(conn, person_id),
                    "watch.updated",
                    organisation=organisation_id,
                    target=("watch", watch_id),
                    summary={
                        "fields": sorted(
                            k for k in changes if k in ("name", "filters", "visibility", "state")
                        )
                    },
                    context=context,
                )
            return self._view(
                self._watch(conn, person_id, organisation_id, watch_id)
                | self._mine(conn, watch_id, person_id)
            )

    def set_email(self, person_id: str, organisation_id: str, watch_id: str, email: str) -> None:
        """A member's own email for a watch they can see (their own, or a shared one)."""
        with self.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            row = self._watch(conn, person_id, organisation_id, watch_id)
            limits = plan_limits(self.accounts.access(organisation_id, conn))
            self._set_email(conn, limits, row, person_id, email)

    def _set_email(self, conn, limits, row, person_id, email):
        self._check_channels(limits, email, row["visibility"] if row["mine"] else "private")
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'watch_member')} (watch_id, person_id, email) VALUES (:w, :p, :e)"
                " ON CONFLICT (watch_id, person_id) DO UPDATE SET email = EXCLUDED.email"
            ),
            {"w": row["id"], "p": person_id, "e": email},
        )

    def delete(self, person_id: str, organisation_id: str, watch_id: str, context=None) -> None:
        """Deleting removes the filters and matches at once (SUB-05: within 24 hours)."""
        with self.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            self._watch(conn, person_id, organisation_id, watch_id, owner=True, lock=True)
            conn.execute(text(f"DELETE FROM {self._t(conn, 'watch')} WHERE id = :w"), {"w": watch_id})
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "watch.deleted",
                organisation=organisation_id,
                target=("watch", watch_id),
                context=context,
            )

    @staticmethod
    def _check_channels(limits, email, visibility):
        if email not in EMAIL:
            raise AccountError("invalid_input", 400, "Email is off, daily or weekly.")
        if email == "daily" and limits["digest"] != "daily":
            raise AccountError("limit_reached", 409, "Daily email comes with paid plans; weekly is included.")
        if visibility not in ("private", "organisation"):
            raise AccountError("invalid_input", 400, "A monitor is private or shared with the organisation.")
        if visibility == "organisation" and not limits["shared"]:
            raise AccountError("limit_reached", 409, "Sharing monitors comes with the Team plan and above.")

    def _mine(self, conn, watch_id, person_id):
        email = conn.scalar(
            text(f"SELECT email FROM {self._t(conn, 'watch_member')} WHERE watch_id = :w AND person_id = :p"),
            {"w": watch_id, "p": person_id},
        )
        return {"my_email": email or "off", "unread": 0}

    def _feed(self, conn) -> Feed:
        return Feed(conn, None)

    @staticmethod
    def _validate(feed: Feed, filters: Filters):
        try:
            feed.validate(filters)
        except FeedError as error:
            raise AccountError(
                error.code, 400, "One of the countries, records or other filters isn't available."
            ) from None

    # ---------- preview and inbox ----------
    def preview(self, person_id: str, organisation_id: str, raw_filters, origin: str) -> dict:
        filters = parse_filters(raw_filters)
        with self.engine.connect() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            feed = Feed(conn, None, origin)
            self._validate(feed, filters)
            coverage = feed.coverage()
            return {
                "events": feed.recent(filters)[:20],
                "feed_started_at": coverage["feed_started_at"],
                "jurisdictions": coverage["jurisdictions"],
                "notice": "A preview of recent changes. A watch notifies only changes after it starts.",
            }

    def inbox(self, person_id, organisation_id, origin, *, watch_id=None, unread=False, limit=50) -> dict:
        with self.engine.connect() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            params = {"p": person_id, "o": organisation_id, "limit": limit}
            where = ""
            if watch_id:
                self._watch(conn, person_id, organisation_id, watch_id)
                where += " AND w.id = :w"
                params["w"] = watch_id
            if unread:
                where += " AND r.inbox_item_id IS NULL"
            events = self.accounts.namespace.qualified(conn, "changes", "change_event")
            rows = conn.execute(
                text(
                    f"SELECT i.id AS item_id, i.matched_at, i.match_reasons, w.id AS watch_id, w.name AS watch_name,"
                    f" r.read_at, e.* FROM {self._t(conn, 'inbox_item')} i"
                    f" JOIN {self._t(conn, 'watch')} w ON w.id = i.watch_id"
                    f" JOIN {events} e ON e.event_id = i.event_id AND e.event_revision = i.event_revision"
                    f" LEFT JOIN {self._t(conn, 'inbox_read')} r ON r.inbox_item_id = i.id AND r.person_id = :p"
                    " WHERE w.organisation_id = :o AND (w.owner_person_id = :p OR w.visibility = 'organisation')"
                    f"{where} ORDER BY i.matched_at DESC, i.id DESC LIMIT :limit"
                ),
                params,
            ).mappings()
            feed, visible = Feed(conn, None, origin), _Visibility(conn)
            items = []
            for row in rows:
                event = feed.project(dict(row), visible)
                if event is None:
                    continue  # no longer public: gone from the inbox too
                items.append(
                    {
                        "id": row["item_id"],
                        "watch": {"id": row["watch_id"], "name": row["watch_name"]},
                        "matched_at": row["matched_at"],
                        "match_reasons": row["match_reasons"],
                        "read": row["read_at"] is not None,
                        "event": event,
                    }
                )
            return {"items": items}

    def mark_read(self, person_id, organisation_id, item_ids=None) -> None:
        with self.engine.begin() as conn:
            self.accounts._membership(conn, person_id, organisation_id)
            params = {"p": person_id, "o": organisation_id}
            only = ""
            if item_ids is not None:
                if (
                    not isinstance(item_ids, list)
                    or len(item_ids) > 500
                    or not all(valid(i, "inb") for i in item_ids)
                ):
                    raise AccountError("invalid_input", 400, "Choose up to 500 items.")
                only, params["ids"] = " AND i.id = ANY(:ids)", item_ids
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'inbox_read')} (inbox_item_id, person_id) SELECT i.id, :p"
                    f" FROM {self._t(conn, 'inbox_item')} i JOIN {self._t(conn, 'watch')} w ON w.id = i.watch_id"
                    " WHERE w.organisation_id = :o AND (w.owner_person_id = :p OR w.visibility = 'organisation')"
                    f"{only} ON CONFLICT DO NOTHING"
                ),
                params,
            )

    # ---------- the person's email preferences ----------
    def preferences(self, person_id: str) -> dict:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(f"SELECT * FROM {self._t(conn, 'notification_pref')} WHERE person_id = :p"),
                    {"p": person_id},
                )
                .mappings()
                .first()
            )
        row = (
            dict(row)
            if row
            else {
                "timezone": "UTC",
                "daily_minute": 420,
                "weekly_day": 1,
                "email_on": True,
                "bounced_at": None,
            }
        )
        return {
            "timezone": row["timezone"],
            "daily_time": f"{row['daily_minute'] // 60:02d}:{row['daily_minute'] % 60:02d}",
            "weekly_day": row["weekly_day"],
            "email_on": row["email_on"],
            "bounced": row["bounced_at"] is not None,
        }

    def set_preferences(self, person_id: str, data: dict) -> dict:
        current = self.preferences(person_id)
        merged = current | {
            k: v for k, v in data.items() if k in ("timezone", "daily_time", "weekly_day", "email_on")
        }
        try:
            ZoneInfo(str(merged["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            raise AccountError("invalid_input", 400, "Choose a time zone from the list.") from None
        clock = TIME.match(str(merged["daily_time"]))
        if not clock:
            raise AccountError("invalid_input", 400, "Give the time as HH:MM.")
        day = merged["weekly_day"]
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 7:
            raise AccountError("invalid_input", 400, "Choose a day of the week.")
        if not isinstance(merged["email_on"], bool):
            raise AccountError("invalid_input", 400, "Email is on or off.")
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'notification_pref')} (person_id, timezone, daily_minute, weekly_day,"
                    " email_on) VALUES (:p, :tz, :m, :d, :on) ON CONFLICT (person_id) DO UPDATE SET timezone ="
                    " EXCLUDED.timezone, daily_minute = EXCLUDED.daily_minute, weekly_day = EXCLUDED.weekly_day,"
                    " email_on = EXCLUDED.email_on, bounced_at = CASE WHEN EXCLUDED.email_on THEN NULL ELSE"
                    f" {self._t(conn, 'notification_pref')}.bounced_at END, updated_at = statement_timestamp()"
                ),
                {
                    "p": person_id,
                    "tz": merged["timezone"],
                    "m": int(clock[1]) * 60 + int(clock[2]),
                    "d": day,
                    "on": merged["email_on"],
                },
            )
        return self.preferences(person_id)

    # ---------- unsubscribe (no session: the token is the credential) ----------
    def issue_token(self, conn, person_id: str, watch_id: str | None) -> str:
        token = secrets.token_urlsafe(32)
        conn.execute(
            text(
                f"INSERT INTO {self._t(conn, 'unsubscribe_token')} (token_hash, person_id, watch_id) VALUES (:h, :p, :w)"
            ),
            {"h": token_hash(token), "p": person_id, "w": watch_id},
        )
        return token

    def _token(self, conn, token: str):
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        return (
            conn.execute(
                text(
                    f"SELECT t.person_id, t.watch_id, w.name AS watch_name FROM {self._t(conn, 'unsubscribe_token')} t"
                    f" LEFT JOIN {self._t(conn, 'watch')} w ON w.id = t.watch_id"
                    " WHERE t.token_hash = :h AND t.expires_at > statement_timestamp()"
                ),
                {"h": token_hash(token)},
            )
            .mappings()
            .first()
        )

    def describe_unsubscribe(self, token: str) -> dict:
        """What the link would stop. Changes nothing (link scanners open links)."""
        with self.engine.connect() as conn:
            row = self._token(conn, token)
        if row is None:
            raise AccountError(
                "not_found", 404, "We couldn't verify this link. Sign in to change your email settings."
            )
        return {"scope": "watch" if row["watch_id"] else "all", "watch_name": row["watch_name"]}

    def unsubscribe(self, token: str) -> dict:
        with self.engine.begin() as conn:
            row = self._token(conn, token)
            if row is None:
                raise AccountError(
                    "not_found", 404, "We couldn't verify this link. Sign in to change your email settings."
                )
            if row["watch_id"]:
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'watch_member')} SET email = 'off' WHERE watch_id = :w AND person_id = :p"
                    ),
                    {"w": row["watch_id"], "p": row["person_id"]},
                )
            else:
                conn.execute(
                    text(
                        f"INSERT INTO {self._t(conn, 'notification_pref')} (person_id, email_on) VALUES (:p, false)"
                        " ON CONFLICT (person_id) DO UPDATE SET email_on = false, updated_at = statement_timestamp()"
                    ),
                    {"p": row["person_id"]},
                )
        return {"scope": "watch" if row["watch_id"] else "all", "watch_name": row["watch_name"], "done": True}
