import json
from pathlib import Path

from schema.identity import IdentityContracts
from schema.models import Corpus
from schema.policy import PolicyContracts
from schema.public import PublicContracts

if __name__ == "__main__":
    Path("schema/corpus.schema.json").write_text(json.dumps(Corpus.model_json_schema(), indent=2) + "\n")
    Path("schema/identity.schema.json").write_text(
        json.dumps(IdentityContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/policy.schema.json").write_text(
        json.dumps(PolicyContracts.model_json_schema(), indent=2) + "\n"
    )
    Path("schema/public.schema.json").write_text(
        json.dumps(PublicContracts.model_json_schema(mode="serialization"), indent=2) + "\n"
    )
