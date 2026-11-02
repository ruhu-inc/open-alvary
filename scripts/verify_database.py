"""Migration/seed smoke check, run against PostgreSQL in CI."""

from api.store import ROOT, Store, engine_for
from rights.policy import public_corpus
from schema.models import Corpus

if __name__ == "__main__":
    catalogue = Corpus.model_validate_json((ROOT / "ingestion/catalogue/ng.json").read_text())
    corpus = public_corpus(Store(engine_for()).load())
    assert {source.id for source in corpus.sources} == {source.id for source in catalogue.sources}
    assert not corpus.versions
    print(f"Database smoke check passed: {len(corpus.sources)} catalogue entries, no public full text.")
