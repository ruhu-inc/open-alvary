"""EVAL-03/04 scoring with hand-calculated fixtures: hits, recall, nDCG, MRR, support, completeness,
abstention, empty denominators and the non-inferiority gate."""

import math
from pathlib import Path

import pytest

from evaluations import records, scoring
from evaluations.records import Case, GateProfile, Judgment, MetricGate
from evaluations.scoring import Delivered

CASE = Case(case_id="c", query="q", route="topic", family="f")


def unit(locator, text="", source="w", kind="provision"):
    return Delivered(source, locator, kind, text)


def test_a_part_inside_a_judged_provision_hits_it_but_a_container_or_document_does_not():
    judged = Judgment(case_id="c", source_id="w", locator="sec_2__subsec_1", grade=3)
    assert scoring.hits(unit("sec_2__subsec_1"), judged) and scoring.hits(
        unit("sec_2__subsec_1__para_a"), judged
    )
    assert not scoring.hits(unit("sec_2"), judged)  # no promotion of a container
    assert not scoring.hits(unit("sec_2__subsec_10"), judged)  # a prefix is not a part
    assert not scoring.hits(unit("sec_2__subsec_1", source="other"), judged)
    assert not scoring.hits(unit("document", kind="document"), judged)
    law = Judgment(case_id="c", source_id="w", locator="document", grade=3)
    assert scoring.hits(unit("document", kind="document"), law) and not scoring.hits(unit("sec_1"), law)


def test_metrics_by_hand():
    judgments = [
        Judgment(case_id="c", source_id="w", locator="sec_1", grade=3, support=("must notify",)),
        Judgment(case_id="c", source_id="w", locator="sec_2", grade=2),
        Judgment(case_id="c", source_id="w", locator="sec_3", grade=1),
    ]
    delivered = [unit("sec_9"), unit("sec_3"), unit("sec_1", "The controller must  notify the Commission.")]
    scores = scoring.score_case(CASE, judgments, delivered)
    assert scores["recall@10"] == 0.5  # sec_1 of the two relevant (sec_3 is only background)
    assert scores["mrr"] == pytest.approx(1 / 3)
    dcg = (2**1 - 1) / math.log2(3) + (2**3 - 1) / math.log2(4)
    ideal = (2**3 - 1) / math.log2(2) + (2**2 - 1) / math.log2(3) + (2**1 - 1) / math.log2(4)
    assert scores["ndcg@10"] == pytest.approx(dcg / ideal)
    assert scores["support@10"] == 0.5 and scores["complete"] == 0.0  # whitespace is normalised
    unsupported = scoring.score_case(CASE, judgments[:1], [unit("sec_1", "The controller may notify.")])
    assert unsupported["recall@10"] == 1.0 and unsupported["support@10"] == 0.0


def test_abstention_and_empty_denominators():
    abstain = Case(case_id="a", query="q", route="unanswerable", expected="abstain", family="f")
    assert scoring.score_case(abstain, [], []) == {"abstain": 1.0}
    assert scoring.score_case(abstain, [], [unit("sec_1")]) == {"abstain": 0.0}
    summary = scoring.aggregate({"a": {"abstain": 1.0}})
    assert summary["abstain"] == {"value": 1.0, "cases": 1}
    assert summary["ndcg@10"] == {"value": None, "cases": 0, "state": "not_evaluable"}  # never 100%


def test_the_gate_passes_equal_runs_and_catches_a_regression():
    cases = [Case(case_id=f"c{n}", query="q", route="topic", family=f"f{n % 3}") for n in range(9)]
    profile = GateProfile(id="g", metrics=(MetricGate(metric="ndcg@10", margin=0.0),), resamples=300)
    baseline = {"cases": {c.case_id: {"ndcg@10": 0.8} for c in cases}}
    assert scoring.gate(baseline, baseline, cases, profile)["state"] == "pass"
    worse = {"cases": {c.case_id: {"ndcg@10": 0.5} for c in cases}}
    decision = scoring.gate(baseline, worse, cases, profile)
    assert decision["state"] == "fail" and decision["metrics"][0]["difference"] == pytest.approx(-0.3)
    one_worse = {"cases": {**baseline["cases"], "c0": {"ndcg@10": 0.0}}}
    assert scoring.gate(baseline, one_worse, cases, profile)["state"] == "inconclusive"
    assert scoring.gate({"cases": {}}, baseline, cases, profile)["state"] == "not_run"


def test_benchmarks_load_and_are_consistent():
    root = Path(__file__).resolve().parent.parent / "benchmarks" / "retrieval"
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        benchmark = records.load(directory)
        assert benchmark.cases and benchmark.manifest.id == directory.name
        assert records.gate_profile(directory) is not None
