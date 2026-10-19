"""RSV-12 in CI: the synthetic smoke benchmark runs through the retrieval service in process, and the run
must not be worse than the committed baseline under the benchmark's gate (margin 0). A deliberate
improvement updates the baseline: WRITE_BASELINE=1 pytest tests/test_postgres_evaluation.py."""

import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.store import ROOT, Store
from evaluations import records, runner, scoring
from operations import publish
from schema.models import Authority, Corpus, Jurisdiction, Language, RightsRecord, Source
from workbench import publication

pytestmark = pytest.mark.postgres
BENCH = ROOT / "benchmarks" / "retrieval" / "synthetic-smoke-1"


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


def publish_synthetic(store):
    texts = json.loads((BENCH / "texts.json").read_text())
    store.save(
        Corpus(
            jurisdictions=[Jurisdiction(id="ng", name="Nigeria", country_code="NG", languages=["en"])],
            authorities=[
                Authority(
                    id="ng-nass", jurisdiction_id="ng", name="National Assembly", kind="legislature",
                    official_url="https://nass.gov.ng/",
                )
            ],
            languages=[Language(id="en", name="English", tag="en")],
            sources=[
                Source(
                    id=t["work"], jurisdiction_id="ng", authority_id="ng-nass", title=t["title"],
                    document_type=t.get("document_type", "act"), citation=t["title"], language="en",
                    canonical_url=f"https://example.org/{t['work']}.pdf", legal_status="current",
                )
                for t in texts
            ],
            rights=[
                RightsRecord(
                    id=f"rights-{t['work']}", source_id=t["work"], metadata_public=True,
                    metadata_basis="Synthetic benchmark record",
                )
                for t in texts
            ],
        )
    )  # fmt: skip
    now = datetime.now(UTC).replace(microsecond=0)
    decision = {
        "document_types": ["act", "regulation"], "rights_holder": "Synthetic", "licence": "Synthetic text",
        "licence_url": None, "attribution_required": False, "attribution_text": None,
        "database_rights_notes": "Synthetic.", "privacy_status": "cleared", "basis": "Synthetic benchmark text.",
        "evidence": ["https://example.org/synthetic"], "decided_by": "Benchmark",
        "decided_at": (now - timedelta(minutes=1)).isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
    }  # fmt: skip
    items = [
        publish.ApprovedText(
            document_id=t["work"], work=t["work"], language="en", document_type=t.get("document_type", "act"),
            text=publication.build({"id": t["work"], "jurisdiction": "ng"}, [{"page": 1, "units": t["units"]}]),
            approver="Benchmark", approved_at=now - timedelta(minutes=5),
            effective_from=date.fromisoformat(t["effective_from"]),
            provenance={"url": f"https://example.org/{t['work']}.pdf", "retrieved_at": "2030-01-01T00:00:00+00:00",
                        "sha256": "7" * 64},
        )
        for t in texts
    ]  # fmt: skip
    publish.publish_texts(store, "ng", publish.Texts(approved=items), decision)


def test_the_synthetic_benchmark_is_no_worse_than_its_baseline(store):
    publish_synthetic(store)
    benchmark = records.load(BENCH)
    record = runner.run(
        benchmark, runner.in_process(store.engine), split="dev", system={"adapter": "in_process"}
    )
    assert not record["failures"], record["failures"]
    if os.environ.get("WRITE_BASELINE"):
        (BENCH / "baseline.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    baseline = json.loads((BENCH / "baseline.json").read_text())
    assert baseline["benchmark"]["digest"] == benchmark.digest, "the benchmark changed: rewrite the baseline"
    decision = scoring.gate(baseline, record, list(benchmark.cases), records.gate_profile(BENCH))
    assert decision["state"] == "pass", json.dumps(decision, indent=2)
    # Every route is exercised, and the abstention cases deliver nothing.
    routes = {c.route for c in benchmark.cases}
    assert routes >= {"citation", "instrument", "definition", "topic", "historical", "unanswerable"}
    assert record["cases"]["s06"] == {"abstain": 1.0} and record["cases"]["s08"] == {"abstain": 1.0}
