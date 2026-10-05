"""Migration/seed smoke check, run against PostgreSQL in CI."""

from api.store import Store, engine_for
from rights.policy import public_corpus

if __name__ == "__main__":
    corpus = public_corpus(Store(engine_for()).load())
    assert len(corpus.sources) == 4
    assert not corpus.versions
    print("Database smoke check passed: four catalogue entries, no public full text.")
