"""The S1 benchmark's synthetic catalogue is valid, labelled as synthetic, and never touches a real
database (scripts/benchmark_catalogue.py)."""

import pytest

from scripts.benchmark_catalogue import fresh_database, percentiles, synthetic


def test_the_synthetic_catalogue_is_labelled_synthetic_and_kept_apart_from_real_law():
    corpus = synthetic(120)
    made = [s for s in corpus.sources if s.id.startswith("synthetic-")]
    assert len(made) == 120 and len(corpus.sources) == 125  # the Nigerian catalogue beside them
    assert all(s.jurisdiction_id == "zz" and "not law" in s.summary for s in made)
    rights = {r.source_id: r for r in corpus.rights}
    assert all(rights[s.id].metadata_basis == "Synthetic benchmark metadata; not law" for s in made)
    assert {a.kind for a in corpus.amendments} >= {"made_under", "repeals", "amends"}
    assert all(a.verified for a in corpus.amendments if a.id.startswith("synthetic-link-"))


def test_only_a_scratch_benchmark_database_is_ever_dropped():
    for url in ("postgresql+psycopg://u@h/open_alvary", "postgresql+psycopg://u@h/oa_scratch"):
        with pytest.raises(SystemExit, match="bench"):
            fresh_database(url)


def test_percentiles_are_read_from_the_sorted_latencies():
    report = percentiles([0.1 * n for n in range(1, 21)])
    assert report == {"count": 20, "p50_ms": 1050.0, "p95_ms": 2000.0, "p99_ms": 2000.0, "max_ms": 2000.0}
