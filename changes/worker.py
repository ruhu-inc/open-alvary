"""The watch worker (SUB C2, C3, changes-contracts.md): match new changes to watches, send digests,
deliver webhooks, apply plan limits. Run every 5 minutes (D19) as a scheduled job; each step holds an advisory lock,
so overlapping runs wait instead of doubling work. At-least-once: matches collapse on their key,
digests on (person, cadence, window start).

  python -m changes.worker run            match, then digests, then plan limits
  python -m changes.worker confirm-epoch  after a feed reset, continue matching from the new epoch
"""

import argparse
import json
import logging
import os
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from accounts import mail
from accounts.ids import new_id
from accounts.store import Accounts
from changes import webhooks
from changes.feed import Feed, _Visibility
from changes.watches import MATCHER, Watches, plan_limits, stored_filters

log = logging.getLogger("open_alvary.changes.worker")
BATCH, PER_RUN, PER_EMAIL = 500, 10_000, 50  # D19 proposals


class EpochChanged(RuntimeError):
    pass


def match(accounts: Accounts) -> int:
    """Append inbox items for changes committed since the checkpoint; returns how many."""
    matched = 0
    with accounts.engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('open-alvary:matcher', 0))"))
        checkpoint_table = accounts._t(conn, "matcher_checkpoint")
        feed = Feed(conn, None)
        epoch = feed.state()["epoch"]
        checkpoint = conn.execute(text(f"SELECT * FROM {checkpoint_table}")).mappings().first()
        if checkpoint is None:  # the first run starts now
            conn.execute(
                text(
                    f"INSERT INTO {checkpoint_table} (epoch, publication_revision, event_id, event_revision)"
                    " VALUES (:e, :r, :id, 2147483647)"
                ),
                {"e": epoch, "r": feed.high(), "id": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
            )
            return 0
        if checkpoint["epoch"] != epoch:
            raise EpochChanged("The change feed was reset: run `python -m changes.worker confirm-epoch`")
        position = (checkpoint["publication_revision"], checkpoint["event_id"], checkpoint["event_revision"])
        high = feed.high()
        watches = [
            dict(w)
            for w in conn.execute(
                text(
                    f"SELECT id, filters, start_revision FROM {accounts._t(conn, 'watch')} WHERE state = 'active'"
                )
            ).mappings()
        ]
        for watch in watches:
            watch["parsed"] = stored_filters(watch["filters"])
        endpoints = webhooks.attached(conn, accounts)  # C3: matches also go to these
        events_table = accounts.namespace.qualified(conn, "changes", "change_event")
        visible, scanned = _Visibility(conn), 0
        while scanned < PER_RUN:
            rows = (
                conn.execute(
                    text(
                        f"SELECT * FROM {events_table} WHERE (publication_revision, event_id, event_revision)"
                        " > (:r, :e, :v) AND publication_revision <= :h"
                        " ORDER BY publication_revision, event_id, event_revision LIMIT :n"
                    ),
                    {"r": position[0], "e": position[1], "v": position[2], "h": high, "n": BATCH},
                )
                .mappings()
                .all()
            )
            for row in rows:
                event = dict(row)
                if watches and feed.project(event, visible) is not None:
                    for watch in watches:
                        if event["publication_revision"] <= watch["start_revision"]:
                            continue
                        reasons = watch["parsed"].matches(event)
                        if reasons is None:
                            continue
                        webhooks.enqueue(conn, accounts, watch["id"], event, endpoints)
                        matched += conn.execute(
                            text(
                                f"INSERT INTO {accounts._t(conn, 'inbox_item')} (id, watch_id, event_id,"
                                " event_revision, match_reasons) VALUES (:id, :w, :e, :v, CAST(:r AS jsonb))"
                                " ON CONFLICT (watch_id, event_id, event_revision) DO NOTHING"
                            ),
                            {
                                "id": new_id("inb"),
                                "w": watch["id"],
                                "e": event["event_id"],
                                "v": event["event_revision"],
                                "r": json.dumps({"matched": reasons, "matcher_revision": MATCHER}),
                            },
                        ).rowcount
                position = (row["publication_revision"], row["event_id"], row["event_revision"])
            scanned += len(rows)
            if len(rows) < BATCH:
                break
        conn.execute(
            text(
                f"UPDATE {checkpoint_table} SET publication_revision = :r, event_id = :e, event_revision = :v,"
                " updated_at = statement_timestamp()"
            ),
            {"r": position[0], "e": position[1], "v": position[2]},
        )
    return matched


def confirm_epoch(accounts: Accounts) -> None:
    """After a feed reset: continue from the new epoch's current end. Changes lost with the
    restore are not notified (SUB recovery)."""
    with accounts.engine.begin() as conn:
        feed = Feed(conn, None)
        conn.execute(text(f"DELETE FROM {accounts._t(conn, 'matcher_checkpoint')}"))
        conn.execute(
            text(
                f"INSERT INTO {accounts._t(conn, 'matcher_checkpoint')} (epoch, publication_revision, event_id,"
                " event_revision) VALUES (:e, :r, :id, 2147483647)"
            ),
            {"e": feed.state()["epoch"], "r": feed.high(), "id": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
        )


# ---------- digests ----------
def local_instant(zone: ZoneInfo, day, minute: int) -> datetime:
    """The UTC instant of a local time; a time skipped by a clock change runs at the next valid
    minute, and a repeated time runs once (its first occurrence)."""
    naive = datetime.combine(day, time(minute // 60, minute % 60))
    for extra in range(0, 181):
        candidate = (naive + timedelta(minutes=extra)).replace(tzinfo=zone, fold=0)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return instant
    return naive.replace(tzinfo=zone).astimezone(UTC)


def last_send(zone: ZoneInfo, minute: int, weekday: int, cadence: str, now: datetime) -> datetime:
    """The most recent scheduled send at or before now."""
    today = now.astimezone(zone).date()
    for back in range(0, 9):
        day = today - timedelta(days=back)
        if cadence == "weekly" and day.isoweekday() != weekday:
            continue
        instant = local_instant(zone, day, minute)
        if instant <= now:
            return instant
    raise ValueError("No scheduled send found")


def digests(accounts: Accounts, mailer: mail.Mailer, base: str, now: datetime | None = None) -> int:
    """Send each person's closed digest windows; returns how many emails were sent."""
    now = now or datetime.now(UTC)
    watches = Watches(accounts)
    sent = 0
    with accounts.engine.connect() as conn:
        people = conn.execute(
            text(
                f"SELECT DISTINCT m.person_id, m.email AS cadence FROM {accounts._t(conn, 'watch_member')} m"
                f" JOIN {accounts._t(conn, 'watch')} w ON w.id = m.watch_id WHERE m.email <> 'off' AND w.state = 'active'"
            )
        ).all()
    for person_id, cadence in people:
        with accounts.engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"open-alvary:digest:{person_id}:{cadence}"},
            )
            sent += _digest(accounts, watches, conn, mailer, base, person_id, cadence, now)
    return sent


def _digest(accounts, watches, conn, mailer, base, person_id, cadence, now) -> int:
    prefs = watches.preferences(person_id)
    person = conn.execute(
        text(f"SELECT email FROM {accounts._t(conn, 'person')} WHERE id = :p AND state = 'active'"),
        {"p": person_id},
    ).first()
    if person is None:
        return 0
    zone = ZoneInfo(prefs["timezone"])
    hours, minutes = (int(x) for x in prefs["daily_time"].split(":"))
    end = last_send(zone, hours * 60 + minutes, prefs["weekly_day"], cadence, now)
    previous = last_send(zone, hours * 60 + minutes, prefs["weekly_day"], cadence, end - timedelta(seconds=1))
    table = accounts._t(conn, "digest")
    last_end = conn.scalar(
        text(f"SELECT max(window_end) FROM {table} WHERE person_id = :p AND cadence = :c"),
        {"p": person_id, "c": cadence},
    )
    # A first digest gathers every match since the person joined (or the window before, if later
    # joined): matches made before a missed send never disappear (changes-and-subscriptions SUB-05).
    joined = conn.scalar(
        text(
            f"SELECT min(created_at) FROM {accounts._t(conn, 'watch_member')} WHERE person_id = :p AND email = :c"
        ),
        {"p": person_id, "c": cadence},
    )
    start = last_end or min(filter(None, (joined, previous)))
    if start >= end:
        return 0
    rows = conn.execute(
        text(
            f"SELECT w.id AS watch_id, w.name AS watch_name, w.organisation_id AS watch_organisation, e.*"
            f" FROM {accounts._t(conn, 'inbox_item')} i"
            f" JOIN {accounts._t(conn, 'watch')} w ON w.id = i.watch_id"
            f" JOIN {accounts._t(conn, 'watch_member')} m ON m.watch_id = w.id AND m.person_id = :p"
            f" JOIN {accounts.namespace.qualified(conn, 'changes', 'change_event')} e"
            " ON e.event_id = i.event_id AND e.event_revision = i.event_revision"
            " WHERE m.email = :c AND w.state = 'active' AND i.matched_at >= :s AND i.matched_at < :end"
            " AND i.matched_at >= m.created_at ORDER BY w.name, e.publication_revision, e.event_id"
        ),
        {"p": person_id, "c": cadence, "s": start, "end": end},
    ).mappings()
    # Each watch's organisation, when keyed, is told of withdrawals what a keyed caller is.
    feeds = {keyed: Feed(conn, None, base, keyed=keyed) for keyed in (False, True)}
    visible, keyed_organisations = _Visibility(conn), {}
    groups: dict[str, dict] = {}
    for row in rows:
        organisation = row["watch_organisation"]
        if organisation not in keyed_organisations:
            keyed_organisations[organisation] = accounts.keyed(organisation, conn)
        event = feeds[keyed_organisations[organisation]].project(dict(row), visible)
        if event is not None:
            groups.setdefault(row["watch_id"], {"name": row["watch_name"], "events": []})["events"].append(
                event
            )
    total = sum(len(g["events"]) for g in groups.values())
    outcome = "empty"
    if total and (not prefs["email_on"] or prefs["bounced"]):
        outcome = "suppressed"
    elif total:
        shown, room = [], PER_EMAIL
        for watch_id, group in groups.items():
            events = group["events"][:room]
            room -= len(events)
            stop = f"{base}/console/unsubscribe/{watches.issue_token(conn, person_id, watch_id)}"
            if events:
                shown.append((group["name"], events, stop))
        stop_all = f"{base}/console/unsubscribe/{watches.issue_token(conn, person_id, None)}"
        message = mail.digest(person.email, cadence, shown, total - (PER_EMAIL - room), base, stop_all)
        outcome = "sent" if mailer.send(message) else "failed"
    conn.execute(
        text(
            f"INSERT INTO {table} (id, person_id, cadence, window_start, window_end, event_count, outcome)"
            " VALUES (:id, :p, :c, :s, :e, :n, :o) ON CONFLICT (person_id, cadence, window_start) DO NOTHING"
        ),
        {"id": new_id("dig"), "p": person_id, "c": cadence, "s": start, "e": end, "n": total, "o": outcome},
    )
    return int(outcome == "sent")


# ---------- plan limits ----------
def enforce_limits(accounts: Accounts, mailer: mail.Mailer, base: str) -> int:
    """After a plan goes down, pause the newest watches above its limit and tell the owners."""
    paused_total = 0
    with accounts.engine.connect() as conn:
        organisations = conn.execute(
            text(
                f"SELECT organisation_id, count(*) FROM {accounts._t(conn, 'watch')} WHERE state = 'active' GROUP BY 1"
            )
        ).all()
    for organisation_id, active in organisations:
        with accounts.engine.begin() as conn:
            limit = plan_limits(accounts.access(organisation_id, conn))["watches"]
            if active <= limit:
                continue
            paused = conn.execute(
                text(
                    f"UPDATE {accounts._t(conn, 'watch')} SET state = 'paused', paused_at = statement_timestamp(),"
                    " revision = revision + 1 WHERE id IN (SELECT id FROM"
                    f" {accounts._t(conn, 'watch')} WHERE organisation_id = :o AND state = 'active'"
                    " ORDER BY created_at DESC LIMIT :n)"
                ),
                {"o": organisation_id, "n": active - limit},
            ).rowcount
            accounts._audit(
                conn,
                ("system", "watch-limits"),
                "watch.paused",
                organisation=organisation_id,
                summary={"count": paused},
            )
            paused_total += paused
        notice = accounts.notice_recipients(organisation_id)
        for to in notice["to"]:
            mailer.send(mail.watches_paused(to, notice["organisation"], paused, base))
    return paused_total


def purge(accounts: Accounts) -> int:
    """Retention (changes-contracts.md, privacy notice): matches 400 days, digest records 30 days,
    unsubscribe links when they expire."""
    removed = 0
    with accounts.engine.begin() as conn:
        for table, condition in (
            ("inbox_item", "matched_at < statement_timestamp() - interval '400 days'"),
            ("digest", "expires_at < statement_timestamp()"),
            ("unsubscribe_token", "expires_at < statement_timestamp()"),
            ("webhook_delivery", "created_at < statement_timestamp() - interval '7 days'"),
        ):
            removed += conn.execute(
                text(f"DELETE FROM {accounts._t(conn, table)} WHERE {condition}")
            ).rowcount
    return removed


def expire(accounts: Accounts) -> int:
    """Record withdrawals caused by expired rights reviews (changes.record.expired), before matching,
    so watches and webhooks hear of them in the same run."""
    from api import text_index
    from api.store import check_legacy_authority
    from changes.record import expired

    with accounts.engine.begin() as conn:
        check_legacy_authority(conn, write=False)
        text_index.ensure(conn)  # a deployment's first run builds the section index if no write has
        return expired(conn)


def run(accounts: Accounts, mailer: mail.Mailer, base: str, hooks: "webhooks.Webhooks | None" = None) -> dict:
    hooks = hooks or webhooks.Webhooks(accounts)
    result = {"expired": expire(accounts)}
    result |= {"matched": match(accounts), "digests": digests(accounts, mailer, base)}
    if webhooks.Sealer.from_environment() is not None or hooks._sealer is not None:
        result["webhooks"] = webhooks.deliver(accounts, hooks, mailer, base)
    else:
        log.warning("Webhook delivery is off: ALVARY_WEBHOOK_KEYS is not set")
    result |= {"paused": enforce_limits(accounts, mailer, base), "purged": purge(accounts)}
    return result


def accounts_for(engine: Engine) -> Accounts:
    from rights.database import PolicyNamespace

    with engine.connect() as conn:
        return Accounts(engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))))


def main(argv=None) -> int:
    from api.store import engine_for

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["run", "confirm-epoch"])
    args = parser.parse_args(argv)
    accounts = accounts_for(engine_for())
    if args.command == "confirm-epoch":
        confirm_epoch(accounts)
        print("Matching continues from the new feed epoch")
        return 0
    base = os.getenv("ALVARY_CONSOLE_BASE_URL", "https://open.alvary.ai").rstrip("/")
    print(run(accounts, mail.Mailer.from_environment(), base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
