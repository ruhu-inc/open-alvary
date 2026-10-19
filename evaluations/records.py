"""Benchmark, case, judgment, gate and run records (EVAL-02, EVAL-05; RSV-12).

A benchmark is a directory: ``manifest.json``, ``cases.jsonl``, ``judgments.jsonl`` and optionally
``gate.json`` and ``baseline.json``. Judgments attach to a work's provision by its locator, which is
stable across texts of the same edition (TXP-04), and name the words that must be delivered for the
evidence to support an answer (``support``), checked against the delivered words at scoring time.
A benchmark whose ``review_state`` is ``development_unreviewed`` can steer development but supports no
quality claim (EVAL-04).
"""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROUTES = ("citation", "instrument", "definition", "topic", "historical", "comparative", "unanswerable")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Manifest(Record):
    id: str
    version: int = Field(ge=1)
    purpose: str
    task_schema: Literal["oa-retrieval-case-1"] = "oa-retrieval-case-1"
    review_state: Literal["development_unreviewed", "reviewed"]
    synthetic: bool
    corpus: str
    splits: tuple[str, ...]
    reviewers: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class Case(Record):
    case_id: str
    query: str = Field(min_length=1, max_length=2000)
    route: Literal[ROUTES]
    expected: Literal["answer", "abstain"] = "answer"
    family: str  # cases of one family are resampled together (EVAL-04)
    split: str = "dev"
    jurisdictions: tuple[str, ...] | None = None
    language: str | None = None
    as_of: date | None = None
    tags: tuple[str, ...] = ()


class Judgment(Record):
    case_id: str
    source_id: str
    locator: str
    grade: int = Field(ge=0, le=3)  # 0 irrelevant, 1 background, 2 useful, 3 directly relevant
    support: tuple[str, ...] = ()  # words that must be delivered to support an answer


class MetricGate(Record):
    metric: str
    margin: float = Field(ge=0)  # the candidate may be at most this much worse (non-inferiority)


class GateProfile(Record):
    id: str
    split: str = "dev"
    metrics: tuple[MetricGate, ...]
    confidence: float = 0.95
    resamples: int = Field(2000, ge=100)
    seed: int = 20261019


class Benchmark(Record):
    manifest: Manifest
    cases: tuple[Case, ...]
    judgments: tuple[Judgment, ...]
    digest: str

    def judged(self, case_id: str) -> list[Judgment]:
        return [j for j in self.judgments if j.case_id == case_id]


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load(directory: Path) -> Benchmark:
    files = [directory / name for name in ("manifest.json", "cases.jsonl", "judgments.jsonl")]
    digest = hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()
    manifest = Manifest.model_validate_json(files[0].read_text())
    cases = tuple(Case.model_validate(c) for c in _lines(files[1]))
    judgments = tuple(Judgment.model_validate(j) for j in _lines(files[2]))
    ids = [c.case_id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case IDs")
    unknown = {j.case_id for j in judgments} - set(ids)
    if unknown:
        raise ValueError(f"Judgments for unknown cases: {sorted(unknown)}")
    for case in cases:
        if case.split not in manifest.splits:
            raise ValueError(f"Case {case.case_id} is in an undeclared split")
        relevant = [j for j in judgments if j.case_id == case.case_id and j.grade >= 2]
        if case.expected == "answer" and not relevant:
            raise ValueError(f"Case {case.case_id} expects an answer but has no relevant judgment")
        if case.expected == "abstain" and relevant:
            raise ValueError(f"Case {case.case_id} expects abstention but has relevant judgments")
    return Benchmark(manifest=manifest, cases=cases, judgments=judgments, digest=digest)


def gate_profile(directory: Path) -> GateProfile | None:
    path = directory / "gate.json"
    return GateProfile.model_validate_json(path.read_text()) if path.exists() else None
