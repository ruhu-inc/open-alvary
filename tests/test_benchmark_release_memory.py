"""The release memory benchmark (scripts/benchmark_release_memory.py) measures a valid release of invented
records, each way; the benchmark itself, at 120,000 records, is not part of the test run."""

import tempfile

import pytest

from exports import integrity
from exports.verify import _rows
from schema.models import Corpus
from scripts.benchmark_release_memory import child, write


def test_the_synthetic_release_is_valid_both_ways_and_says_it_is_not_law(tmp_path):
    write(tmp_path, 30, 600, 4)
    assert integrity.valid(lambda name: _rows(tmp_path, name))
    corpus = Corpus.model_validate({name: list(_rows(tmp_path, name)) for name in Corpus.model_fields})
    assert len(corpus.sources) == 30 and len(corpus.structure) == 120
    assert all("not law" in source.title for source in corpus.sources)


@pytest.mark.parametrize("mode", ["at_once", "row_at_a_time"])
def test_each_way_reports_what_it_measured(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)  # the measured way points it at its scratch
    write(tmp_path, 5, 300, 2)
    report = child(mode, tmp_path, traced=False)
    assert report["valid"] and report["peak_rss_mb"] > 0
    assert ("scratch_largest_mb" in report) == (mode == "row_at_a_time")
