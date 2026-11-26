"""Once, right after the release that brings seat limits (migration 0059) has run scripts.seed: give every
organisation that then has more members than its plan's seats 30 days' grace, and email its owners
(teams-and-seats §5.3, the 30 days' notice of the terms). Nobody is removed. Every plan counts: Starter,
Developer (Pro) and Law check have 1 seat; pilot, Team and Public interest 5.

An organisation that already has a grace keeps it and is not emailed again, so a second run changes nothing.
It runs as the owner's DATABASE_URL, with RESEND_API_KEY for the mail (without one, messages are only logged).

    python -m scripts.seat_grace            give the grace and send the emails
    python -m scripts.seat_grace --dry-run  list how many organisations it would reach, changing nothing
"""

import argparse
import json
import os

from sqlalchemy import text

from accounts import mail
from accounts.store import Accounts
from api.store import engine_for
from rights.database import PolicyNamespace

NOTICE_DAYS = 30


def give_grace(accounts: Accounts, mailer: mail.Mailer | None, base: str, *, dry_run: bool = False) -> dict:
    done = {"over_limit": 0, "graces": 0, "emails": 0}
    with accounts.engine.connect() as conn:
        organisations = (
            conn.execute(
                text(
                    f"SELECT o.id, o.name, o.seat_grace_until,"
                    f" (SELECT count(*) FROM {accounts._t(conn, 'membership')} m WHERE m.organisation_id = o.id) AS members,"
                    f" {accounts._fn(conn, 'organisation_seat_limit')}(o.id) AS seats"
                    f" FROM {accounts._t(conn, 'organisation')} o WHERE o.state <> 'deleted' ORDER BY o.id"
                )
            )
            .mappings()
            .all()
        )
    for organisation in organisations:
        if organisation["members"] <= organisation["seats"]:
            continue
        done["over_limit"] += 1
        if dry_run or organisation["seat_grace_until"] is not None:
            continue
        with accounts.engine.begin() as conn:
            until = accounts._sync_seat_grace(conn, organisation["id"], grace_days=NOTICE_DAYS)
        done["graces"] += 1
        if mailer is None or until is None:
            continue
        delivered = 0
        for to in accounts.notice_recipients(organisation["id"])["to"]:
            delivered += bool(
                mailer.send(
                    mail.seats_over_limit(
                        to, organisation["name"], organisation["members"], organisation["seats"], until, base
                    )
                )
            )
        done["emails"] += delivered
        if delivered:
            # Recorded as the daily job records its own (accounts.billing seat_notices), so that it does not
            # tell the same owners again. With nothing delivered (no mail key), the daily job sends it.
            with accounts.engine.begin() as conn:
                accounts._audit(
                    conn,
                    ("system", "seats"),
                    "seats.notice_sent",
                    organisation=organisation["id"],
                    target=("organisation", organisation["id"]),
                    summary={
                        "members": organisation["members"],
                        "seats": organisation["seats"],
                        "until": until.isoformat(),
                    },
                )
    return done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true")
    dry_run = parser.parse_args(argv).dry_run
    engine = engine_for()
    with engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    accounts = Accounts(engine, namespace)
    base = os.getenv("ALVARY_CONSOLE_BASE_URL", "https://open.alvary.ai")
    print(json.dumps(give_grace(accounts, mail.Mailer.from_environment(), base, dry_run=dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
