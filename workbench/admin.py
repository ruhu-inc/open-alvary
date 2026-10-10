"""Manage workbench operators (run on the server; there is no web route that adds operators).

python -m workbench.admin add ijidai@ruhu.ai --name "Ijidai Lassa" --roles reviewer,approver
python -m workbench.admin deactivate someone@example.org
python -m workbench.admin list
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
    add.add_argument("--roles", required=True, help="comma-separated: reviewer, approver, admin")
    off = sub.add_parser("deactivate")
    off.add_argument("email")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    settings.load()
    store = Store(engine_from_environment())
    if args.action == "add":
        store.add_operator(args.email, args.name, {r.strip() for r in args.roles.split(",") if r.strip()})
    elif args.action == "deactivate":
        store.deactivate(args.email)
    else:
        with store.engine.connect() as db:
            for row in db.execute(select(operator).order_by(operator.c.email)):
                print(f"{row.email:40} {row.roles:26} {'active' if row.active else 'inactive'}  {row.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
