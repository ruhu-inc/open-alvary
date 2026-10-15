"""Operator access actions from the command line (MAC-11), until the workbench's Access section.

    requests                         open access requests, oldest first
    grant ORG --reason R [--tier pilot] [--jurisdictions ng,rw] [--until 2027-01-14]
          [--waiver] [--request wrq_…]
                                     grant (or change) an organisation's access from now
    decline ORG REQUEST --reason R   decline a request; it can be renewed after 30 days
    ask ORG REQUEST --reason R       ask the requester for more information
    revoke ORG --reason R            revoke an organisation's access

Every action is in the accounts audit log with the operator named by --operator. Needs
DATABASE_URL for the PostgreSQL database that holds the accounts schema.
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from accounts import mail
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
    grant = commands.add_parser("grant")
    grant.add_argument("organisation")
    grant.add_argument("--reason", required=True)
    grant.add_argument("--tier", default="pilot")
    grant.add_argument("--jurisdictions", help="Comma-separated jurisdiction IDs; all when omitted")
    grant.add_argument("--until", help="End date (UTC), YYYY-MM-DD; no end when omitted")
    grant.add_argument("--waiver", action="store_true", help="Free or reduced access (otherwise manual)")
    grant.add_argument("--request", help="The access request this grant answers")
    for name in ("decline", "ask"):
        decision = commands.add_parser(name)
        decision.add_argument("organisation")
        decision.add_argument("request")
        decision.add_argument("--reason", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("organisation")
    revoke.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
        raise SystemExit("DATABASE_URL must name the PostgreSQL database with the accounts schema")
    engine = engine_for()
    with engine.connect() as conn:
        accounts = Accounts(engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))))
    operator = args.operator.strip()
    mailer = mail.Mailer.from_environment()
    site = os.getenv("ALVARY_CONSOLE_BASE_URL", "https://open.alvary.ai").rstrip("/")

    def tell(organisation, request, build):
        notice = accounts.notice_recipients(organisation, request)
        for to in notice["to"]:
            mailer.send(build(to, notice["organisation"]))

    try:
        if args.command == "requests":
            for request in accounts.open_requests():
                print(json.dumps(request, default=str, indent=2))
            return 0
        if args.command == "grant":
            scope = (
                {"jurisdictions": [j.strip() for j in args.jurisdictions.split(",") if j.strip()]}
                if args.jurisdictions
                else {"all": True}
            )
            until = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=UTC) if args.until else None
            entitlement = accounts.grant_access(
                operator,
                args.organisation,
                tier=args.tier,
                scope=scope,
                source="waiver" if args.waiver else "manual",
                ends_at=until,
                reason=args.reason,
                request_id=args.request,
            )
            print(f"Granted {entitlement}")
            until = f"{until:%-d %B %Y}" if until else None
            tell(args.organisation, args.request, lambda to, name: mail.access_granted(to, name, until, site))
        elif args.command in ("decline", "ask"):
            accounts.decide_request(
                operator,
                args.organisation,
                args.request,
                "declined" if args.command == "decline" else "needs_information",
                args.reason,
            )
            print("Recorded")
            renewable = f"{datetime.now(UTC) + timedelta(days=30):%-d %B %Y}"
            tell(
                args.organisation,
                args.request,
                (
                    (lambda to, name: mail.access_declined(to, name, args.reason, renewable, site))
                    if args.command == "decline"
                    else (lambda to, name: mail.more_information(to, name, args.reason, site))
                ),
            )
        else:
            accounts.revoke_access(operator, args.organisation, args.reason)
            print("Revoked")
            tell(args.organisation, None, lambda to, name: mail.access_revoked(to, name, site))
    except AccountError as error:
        raise SystemExit(f"{error.code}: {error.message}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
