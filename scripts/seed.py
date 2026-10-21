import json
from pathlib import Path

from sqlalchemy import text

from accounts.store import Accounts
from api.store import Store, engine_for, seed
from changes.record import sweep
from operations import memberships
from rights.database import PolicyNamespace

TIERS = Path(__file__).resolve().parents[1] / "accounts" / "tiers.json"

if __name__ == "__main__":
    engine = engine_for()
    seed(Store(engine))
    print("Nigeria catalogue seeded; no legal full text imported.")
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        Accounts(engine, namespace).set_tiers(json.loads(TIERS.read_text()))
        print("Access tiers in line with accounts/tiers.json.")
        # Each release publishes the membership register's reviewed rows, as it serves the reviewed court
        # registers it holds; proposals are never published (JUR-04).
        report = memberships.publish(engine)
        counts = ", ".join(f"{n} {k}" for k, n in report.items() if k in ("published", "updated", "removed"))
        print(f"Memberships in line with schema/memberships.json: {counts}.")
        with engine.begin() as conn:
            print(f"Change log swept: {sweep(conn)} changes recorded.")
