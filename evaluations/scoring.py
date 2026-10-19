"""Scoring and gates (EVAL-03, EVAL-04; RSV-12): what each metric counts, over what, and how a candidate
configuration is compared with the active one.

Every metric is scored per case over the evidence actually delivered (stage ``final``), then averaged
over the cases where it is defined; a metric with no defined case is ``not_evaluable``, never 100%.

- A delivered unit **hits** a judged provision when it is that provision or a part inside it, in the
  same work. A whole law's document unit never hits a provision; a containing provision does not
  count as hitting the part inside it (no promotion of a container into a part). A judgment on the
  locator ``document`` (an instrument question) is hit only by that law's document unit.
- ``recall@k``: relevant provisions (grade 2-3) hit in the first k units / relevant provisions.
- ``ndcg@10``: gains (2^grade - 1) / log2(rank + 1) for the first hit of each judged provision, over the
  ideal ordering of the judged grades; each provision earns its gain once.
- ``mrr``: 1 / the rank of the first unit hitting a relevant provision; 0 when none does.
- ``support@10``: relevant provisions hit in the first 10 units whose ``support`` words all occur in the
  words delivered for that work / relevant provisions.
- ``complete``: 1 when every relevant provision is supported, else 0.
- ``abstain``: for a case that expects abstention, 1 when no evidence was delivered, else 0.

Gates compare paired per-case scores with a bootstrap over case families (EVAL-04): a metric passes
when the lower confidence bound of (candidate - baseline) is above -margin, fails when the upper bound
is below -margin, and is otherwise inconclusive. Inconclusive never promotes (EVAL-06).
"""

import math
import random
import re
from dataclasses import dataclass

from evaluations.records import Case, GateProfile, Judgment

METRICS = ("recall@10", "recall@20", "ndcg@10", "mrr", "support@10", "complete", "abstain")


@dataclass(frozen=True)
class Delivered:
    source_id: str
    locator: str
    unit: str
    text: str


def hits(unit: Delivered, judgment: Judgment) -> bool:
    if unit.source_id != judgment.source_id:
        return False
    if judgment.locator == "document":  # an instrument question: the law itself is the evidence
        return unit.unit == "document"
    return unit.unit != "document" and (
        unit.locator == judgment.locator or unit.locator.startswith(judgment.locator + "__")
    )


def _words(text: str) -> str:
    return " ".join(re.sub(r"[“”\"‘’']", '"', text).split()).casefold()


def score_case(case: Case, judgments: list[Judgment], delivered: list[Delivered]) -> dict[str, float]:
    if case.expected == "abstain":
        return {"abstain": 0.0 if delivered else 1.0}
    relevant = [j for j in judgments if j.grade >= 2]
    judged = [j for j in judgments if j.grade >= 1]

    def recall(k: int) -> float:
        found = {id(j) for j in relevant if any(hits(u, j) for u in delivered[:k])}
        return len(found) / len(relevant)

    credited: set[int] = set()
    dcg = 0.0
    for rank, unit in enumerate(delivered[:10], start=1):
        best = max(
            (j for j in judged if id(j) not in credited and hits(unit, j)),
            key=lambda j: j.grade,
            default=None,
        )
        if best is not None:
            credited.add(id(best))
            dcg += (2**best.grade - 1) / math.log2(rank + 1)
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(sorted((j.grade for j in judged), reverse=True)[:10], start=1)
    )
    first = next(
        (rank for rank, u in enumerate(delivered, start=1) if any(hits(u, j) for j in relevant)), None
    )
    supported = 0
    for judgment in relevant:
        if not any(hits(u, judgment) for u in delivered[:10]):
            continue
        words = _words(" ".join(u.text for u in delivered if u.source_id == judgment.source_id))
        if all(_words(phrase) in words for phrase in judgment.support):
            supported += 1
    return {
        "recall@10": recall(10),
        "recall@20": recall(20),
        "ndcg@10": dcg / ideal if ideal else 0.0,
        "mrr": 1.0 / first if first else 0.0,
        "support@10": supported / len(relevant),
        "complete": 1.0 if supported == len(relevant) else 0.0,
    }


def aggregate(per_case: dict[str, dict[str, float]]) -> dict[str, dict]:
    """Each metric's macro mean over the cases where it is defined, with the count."""
    out = {}
    for metric in METRICS:
        values = [scores[metric] for scores in per_case.values() if metric in scores]
        out[metric] = (
            {"value": round(sum(values) / len(values), 6), "cases": len(values)}
            if values
            else {"value": None, "cases": 0, "state": "not_evaluable"}
        )
    return out


def paired(
    baseline: dict[str, float], candidate: dict[str, float], families: dict[str, str], profile: GateProfile
) -> tuple[float, float, float, int] | None:
    """The mean of candidate - baseline over the cases both scored, with its bootstrap interval over
    case families; None when no case was scored by both."""
    common = sorted(set(baseline) & set(candidate))
    if not common:
        return None
    by_family: dict[str, list[float]] = {}
    for case_id in common:
        by_family.setdefault(families[case_id], []).append(candidate[case_id] - baseline[case_id])
    groups = list(by_family.values())
    mean = sum(d for g in groups for d in g) / len(common)
    rng = random.Random(profile.seed)
    means = []
    for _ in range(profile.resamples):
        sample = [groups[rng.randrange(len(groups))] for _ in groups]
        size = sum(len(g) for g in sample)
        means.append(sum(d for g in sample for d in g) / size)
    means.sort()
    tail = (1 - profile.confidence) / 2
    low = means[int(math.floor(tail * (len(means) - 1)))]
    high = means[int(math.ceil((1 - tail) * (len(means) - 1)))]
    return mean, low, high, len(common)


def gate(baseline_run: dict, candidate_run: dict, cases: list[Case], profile: GateProfile) -> dict:
    """Non-inferiority of the candidate against the baseline on each gated metric (EVAL-04, RSV-12)."""
    families = {c.case_id: c.family for c in cases if c.split == profile.split}
    results, states = [], []
    for item in profile.metrics:
        base = {
            k: v[item.metric] for k, v in baseline_run["cases"].items() if k in families and item.metric in v
        }
        cand = {
            k: v[item.metric] for k, v in candidate_run["cases"].items() if k in families and item.metric in v
        }
        found = paired(base, cand, families, profile)
        if found is None:
            state, detail = "not_run", {}
        else:
            mean, low, high, n = found
            state = "pass" if low >= -item.margin else "fail" if high < -item.margin else "inconclusive"
            detail = {"difference": round(mean, 6), "low": round(low, 6), "high": round(high, 6), "cases": n}
        results.append({"metric": item.metric, "margin": item.margin, "state": state, **detail})
        states.append(state)
    overall = (
        "fail"
        if "fail" in states
        else "inconclusive"
        if "inconclusive" in states
        else "not_run"
        if "not_run" in states
        else "pass"
    )
    return {"profile": profile.id, "state": overall, "metrics": results}
