import json
from pathlib import Path

from sqlalchemy import text

from accounts.store import Accounts
from api.store import Store, engine_for, seed
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
