"""Manage workbench operators (run on the server; there is no web route that adds operators).

python -m workbench.admin add reviewer@example.org --name "Ada Reviewer" --roles reviewer,approver
python -m workbench.admin deactivate someone@example.org
python -m workbench.admin list
python -m workbench.admin enrol someone@example.org   a one-time code (24 hours) to register a passkey
python -m workbench.admin enrol someone@example.org --replace   ...after revoking their passkeys (one lost)
"""

import argparse

from sqlalchemy import select

from ingestion import settings
from workbench.store import Store, engine_from_environment, operator


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("email")
    add.add_argument("--name", required=True)
    add.add_argument(
        "--roles", required=True, help="comma-separated: reviewer, approver, admin, access_admin"
    )
    off = sub.add_parser("deactivate")
    off.add_argument("email")
    sub.add_parser("list")
    enrol = sub.add_parser("enrol")
    enrol.add_argument("email")
    enrol.add_argument("--replace", action="store_true", help="Revoke the operator's passkeys first")
    impact = sub.add_parser(
        "impact", help="what a newly prepared copy of the collections would do to review work"
    )
    impact.add_argument("places", help="the new copy's collections, as ALVARY_REVIEW_COLLECTIONS names them")
    carry = sub.add_parser(
        "carry",
        help="a renamed document's page checks carried to its successor, where its pages read the same",
    )
    carry.add_argument("old", help="the document's id before it was renamed")
    carry.add_argument("new", help="its id now, in the collections the workbench reads")
    carry.add_argument(
        "--apply", action="store_true", help="write them; without it, only list what would carry"
    )
    unchanged = sub.add_parser(
        "carry-unchanged",
        help="page checks kept where only a page's flags changed, its text the same and no medium or "
        "high flag new",
    )
    unchanged.add_argument("previous", help="the copy the checks were made in: <place>[:<place>…]")
    unchanged.add_argument(
        "--apply", action="store_true", help="write them; without it, only list what would carry"
    )
    args = parser.parse_args(argv)
    settings.load()
    store = Store(engine_from_environment())
    if args.action == "add":
        store.add_operator(args.email, args.name, {r.strip() for r in args.roles.split(",") if r.strip()})
    elif args.action == "deactivate":
        store.deactivate(args.email)
    elif args.action == "impact":
        import json

        from schema.jurisdictions import registry
        from workbench import impact

        current, new = impact.libraries(args.places)
        found = impact.report(store, current, new, registry())
        print(json.dumps(found["summary"], ensure_ascii=False))
        for key in ("documents_gone", "changed", "approvals_lapsing", "details_orphaned"):
            for row in found[key]:
                print(json.dumps({key: row}, ensure_ascii=False))
    elif args.action == "carry":
        import json

        from workbench import content, impact

        found = impact.carry(store, content.Library(), args.old, args.new, apply=args.apply)
        print(json.dumps({"old": args.old, "new": args.new, "applied": args.apply, **found}))
    elif args.action == "carry-unchanged":
        import json

        from workbench import impact

        library, previous = impact.libraries(args.previous)
        found = impact.carry_unchanged(store, previous, library, apply=args.apply)
        counts = {kind: sum(map(len, pages.values())) for kind, pages in found.items()}
        print(json.dumps({"previous": args.previous, "applied": args.apply, "pages": counts, **found}))
    elif args.action == "enrol":
        if args.replace:
            for key in store.passkeys(args.email.strip().lower()):
                store.revoke_passkey(args.email.strip().lower(), key["credential_id"])
        # Give the code to the operator yourself; it registers a passkey for the Access section.
        print(
            f"Passkey enrolment code for {args.email} (one use, 24 hours): {store.issue_enrolment(args.email)}"
        )
    else:
        with store.engine.connect() as db:
            for row in db.execute(select(operator).order_by(operator.c.email)):
                print(f"{row.email:40} {row.roles:26} {'active' if row.active else 'inactive'}  {row.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
