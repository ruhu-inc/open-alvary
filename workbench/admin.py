"""Manage workbench operators (run on the server; there is no web route that adds operators).

python -m workbench.admin add ijidai@ruhu.ai --name "Ijidai Lassa" --roles reviewer,approver
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
