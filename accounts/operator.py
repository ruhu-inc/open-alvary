"""Operator access actions from the command line (MAC-11), beside the workbench's Access section;
both run accounts.access_admin.

    requests                         open access requests, oldest first
    show ORG                         an organisation: access, members, keys, apps, usage, audit
    find TEXT                        organisations by name, ID, or a member's email
    grant ORG --reason R [--tier pilot] [--jurisdictions ng,rw] [--until 2027-01-14]
          [--waiver] [--request wrq_…] [--evidence E]
                                     grant (or change) an organisation's access from now
    decline ORG REQUEST --reason R   decline a request; it can be renewed after 30 days
    ask ORG REQUEST --reason R       ask the requester for more information
    revoke ORG --reason R            revoke an organisation's access
    suspend|restore organisation ORG --reason R [--evidence E]
    suspend|restore person PERSON --reason R [--evidence E]
    restore-owner ORG MEMBERSHIP --reason R --evidence E
                                     make a member the owner where no active person owns it
    revoke-credential KEY_OR_ID --reason R [--evidence E]
                                     a leaked key (or its ID), or a connected app's grant ID

Every action is in the accounts audit log with the operator named by --operator. Needs
DATABASE_URL for the PostgreSQL database that holds the accounts schema.
"""

import argparse
import json
import os
from datetime import UTC, datetime

from sqlalchemy import text

from accounts import mail
from accounts.access_admin import AccessAdmin, Action
from accounts.store import AccountError, Accounts
from api.store import engine_for
from rights.database import PolicyNamespace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--operator", required=True, help="Who is acting, as recorded in the audit log")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("requests")
    commands.add_parser("show").add_argument("organisation")
    commands.add_parser("find").add_argument("text")

    def change(name):
        command = commands.add_parser(name)
        command.add_argument("--reason", required=True)
        command.add_argument("--evidence")
        return command

    grant = change("grant")
    grant.add_argument("organisation")
    grant.add_argument("--tier", help="The plan to grant; by default the plan the request asked for")
    grant.add_argument(
        "--jurisdictions", help="Comma-separated country codes; by default the request's, else all"
    )
    grant.add_argument("--until", help="End date (UTC), YYYY-MM-DD; no end when omitted")
    grant.add_argument("--waiver", action="store_true", help="Free or reduced access (otherwise manual)")
    grant.add_argument("--request", help="The access request this grant answers")
    for name in ("decline", "ask"):
        decision = change(name)
        decision.add_argument("organisation")
        decision.add_argument("request")
    change("revoke").add_argument("organisation")
    for name in ("suspend", "restore"):
        state = change(name)
        state.add_argument("kind", choices=["organisation", "person"])
        state.add_argument("id")
    owner = change("restore-owner")
    owner.add_argument("organisation")
    owner.add_argument("membership")
    change("revoke-credential").add_argument("credential")
    args = parser.parse_args(argv)

    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        raise SystemExit("DATABASE_URL must name the PostgreSQL database with the accounts schema")
    engine = engine_for()
    with engine.connect() as conn:
        accounts = Accounts(engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))))
    admin = AccessAdmin(accounts, mail.Mailer.from_environment())

    def show(value) -> None:
        print(json.dumps(value, default=str, indent=2))

    try:
        if args.command == "requests":
            show(admin.requests())
            return 0
        if args.command == "show":
            show(admin.organisation(args.organisation))
            return 0
        if args.command == "find":
            show(admin.search(args.text))
            return 0
        action = Action(args.operator.strip(), args.reason, args.evidence)
        if args.command == "grant":
            until = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=UTC) if args.until else None
            jurisdictions = args.jurisdictions.split(",") if args.jurisdictions else None
            entitlement = admin.grant(
                action,
                args.organisation,
                tier=args.tier,
                jurisdictions=jurisdictions,
                until=until,
                waiver=args.waiver,
                request_id=args.request,
            )
            print(f"Granted {entitlement}")
        elif args.command in ("decline", "ask"):
            decision = "declined" if args.command == "decline" else "needs_information"
            admin.decide(action, args.organisation, args.request, decision)
            print("Recorded")
        elif args.command == "revoke":
            admin.revoke(action, args.organisation)
            print("Revoked")
        elif args.command in ("suspend", "restore"):
            if args.kind == "organisation":
                print(f"Now {admin.set_organisation(action, args.id, args.command)}")
            else:
                admin.set_person(action, args.id, args.command)
                print("Recorded")
        elif args.command == "restore-owner":
            admin.restore_owner(action, args.organisation, args.membership)
            print("Owner restored")
        else:
            show(admin.revoke_credential(action, args.credential))
    except AccountError as error:
        raise SystemExit(f"{error.code}: {error.message}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
