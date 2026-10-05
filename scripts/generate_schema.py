import json
from pathlib import Path

from schema.models import Corpus

if __name__ == "__main__":
    Path("schema/corpus.schema.json").write_text(json.dumps(Corpus.model_json_schema(), indent=2) + "\n")
