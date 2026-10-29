"""Each work's first draft of details, measured before anyone reviews it (workbench.details_audit;
owner, 2026-10-29): where each value comes from, which required fields start empty, whether the
reader's suggestions were used at all, and whether a second source agrees."""

from schema.jurisdictions import registry
from workbench import details_audit as A

SHA = "473bb8c4b5a58c0fc123400669cbf18535da586b97140767005c3d7867a3455c"
RLRC = "https://www.rlrc.gov.rw/fileadmin/user_upload/RLRC/Laws_of_Rwanda_v2/"
MANIFEST = {"jurisdiction": "rw", "documents": []}


def rwanda(**more):
    return {
        "id": "rw-x-en",
        "work": "rw-x",
        "jurisdiction": "rw",
        "language": "en",
        "source_object": f"sources/rw/rlrc-laws/{SHA}.pdf",
        **more,
    }


def test_a_treaty_named_from_its_file_keeps_its_suggestions_its_family_put_right():
    treaty = rwanda(
        type="agreement",
        title="African medicine agency fn2",
        url=RLRC + "International_Legal_Instruments__ILI_/7.8.2_African_Medicine_Agency_fn2.pdf",
        issue={"name": "7.8.2_African_Medicine_Agency_fn2.pdf"},
        suggested={
            "identity.family": "executive_instrument",  # not a valid pair with "agreement"
            "identity.type": "agreement",
            "identity.title": "African medicine agency fn2",
            "identity.number.value": SHA[:12],
        },
    )
    report = A.audit_work(treaty, MANIFEST, registry())
    assert report["suggestions"] == 4 and report["suggestions_used"] and report["suggestions_repaired"]
    assert report["family"] == "treaty" and "profile.depositary" in report["missing_required"]
    assert report["fields"]["identity.title"] == "file_name"
    assert report["fields"]["identity.number.value"] == "fingerprint"
    assert report["second_source"] == {"heading": "not_found"}
    assert A.from_fingerprint(treaty, SHA[:12]) and A.from_fingerprint(treaty, f"2019-{SHA[:6]}")


def test_a_law_read_from_its_heading_is_checked_against_its_file_name():
    law = rwanda(
        type="law",
        title="Law n° 055/2024 of 20/06/2024 on the protection of intellectual property",
        url=RLRC + "Laws_in_force/3._Economy/3.1._LAW_N_055_2024_OF_20_06_2024.pdf",
        issue={"name": "3.1._LAW_N_055_2024_OF_20_06_2024.pdf"},
        suggested={
            "identity.family": "legislation",
            "identity.type": "law",
            "identity.title": "Law n° 055/2024 of 20/06/2024 on the protection of intellectual property",
            "identity.number.value": "055/2024",
            "identity.year": "2023",
        },
    )
    report = A.audit_work(law, MANIFEST, registry())
    assert report["suggestions_used"] and report["fields"]["identity.title"] == "reader"
    assert report["second_source"]["number"] == "agree" and report["second_source"]["year"] == "disagree"


def test_a_judgment_shows_which_values_carry_their_words():
    judgment = {
        "id": "ng-nicn-la-1-2026-01-27",
        "jurisdiction": "ng",
        "type": "judgment",
        "title": "A -VS- B",
        "source_object": "sources/ng/nicn-judgments/abc.html",
        "suggested": {
            "identity.family": "judgment",
            "identity.type": "judgment",
            "identity.title": "A -VS- B",
            "identity.number.value": "NICN/LA/1/2025",
        },
        "suggested_evidence": {
            "identity.title": {"value": "A -VS- B", "page": None, "evidence": "listing"},
            "identity.number.value": {
                "value": "NICN/LA/1/2025",
                "page": 1,
                "evidence": "SUIT NO. NICN/LA/1/2025",
            },
        },
    }
    report = A.audit_work(judgment, {"jurisdiction": "ng"}, registry())
    assert report["fields"]["identity.number.value"] == "printed"
    assert report["fields"]["identity.title"] == "catalogue"
    act = {"id": "ng-act-1-2024", "jurisdiction": "ng", "type": "act"}
    assert A.audit_work(act, {"jurisdiction": "ng"}, registry()) == {
        "id": "ng-act-1-2024",
        "drafted_from_reading": True,
    }


def test_one_document_per_work_and_a_summary():
    documents = [
        {"id": "rw-x-rw", "work": "rw-x", "language": "rw"},
        {"id": "rw-x-en", "work": "rw-x", "language": "en"},
        {"id": "rw-x-annex-1-en", "work": "rw-x", "part": "annex-1", "language": "en"},
        {"id": "rw-y-fr", "work": "rw-y", "language": "fr"},
    ]
    assert [d["id"] for d in A.works(documents)] == ["rw-x-en", "rw-y-fr"]
    treaty = A.audit_work(
        rwanda(
            type="agreement",
            title="X",
            suggested={"identity.family": "executive_instrument", "identity.type": "agreement"},
        ),
        MANIFEST,
        registry(),
    )
    summary = A.summarise([treaty, {"id": "ng-act", "drafted_from_reading": True}])
    assert summary["works"] == 1 and summary["drafted_from_reading"] == 1
    assert summary["totals"]["suggestions_dropped"] == 0 and summary["totals"]["suggestions_repaired"] == 1
    assert summary["totals"]["missing_any_required"] == 1
    assert "rw/rlrc-laws/agreement" in summary["by_source_and_type"]
