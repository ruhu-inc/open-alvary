"""Membership of the African Union and the regional bodies (JUR-01, JUR-04): dated, evidenced relationships
that become facts only when a reviewer approves them; a proposal is never a fact. Dates may be uncertain
(a month, a year, a window): a day inside the window is unknown, never guessed. A suspension from
participation is context: it never changes whether a membership is held. The examples are synthetic:
their dates and evidence are fixtures, not research."""

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from api.catalogue import bodies_on, membership_list
from operations import memberships
from operations.publish import Refused
from schema.jurisdictions import catalogue_entry
from schema.memberships import Membership, MembershipRegister, Suspension, register
from schema.models import Jurisdiction
from schema.public import MembershipView, SuspensionView

AT = datetime(2026, 10, 21, 9, tzinfo=UTC)
BASIS = {
    "kind": "entry_into_force",
    "instrument": "Synthetic Treaty",
    "pinpoint": "art. 1",
    "evidence_url": "https://evidence.example.org/treaty",
}
LEGAL_BASIS = (
    {"instrument": "Synthetic Act", "pinpoint": "art. 2", "evidence_url": "https://evidence.example.org/act"},
)


def row(member, body, start, end=None, *, n=1, status="reviewed", **extra):
    reviewed = {"reviewed_by": "Test Reviewer", "reviewed_at": AT} if status == "reviewed" else {}
    return Membership(
        id=f"{member}:{body}:{n}",
        subject_id=member,
        target_id=body,
        status=status,
        valid_from=start,
        valid_to=end,
        evidence_url="https://evidence.example.org/members",
        note="Synthetic fixture.",
        **({"basis": BASIS if status == "reviewed" else None} | reviewed | extra),
    )


def suspension(membership_id, start, end=None, *, n=1, status="reviewed", **extra):
    reviewed = {"reviewed_by": "Test Reviewer", "reviewed_at": AT} if status == "reviewed" else {}
    defaults = {
        "legal_basis": LEGAL_BASIS,
        "lifted_evidence_url": "https://evidence.example.org/lifted" if end else None,
    }
    return Suspension(
        id=f"{membership_id}:s{n}",
        membership_id=membership_id,
        status=status,
        valid_from=start,
        valid_to=end,
        evidence_url="https://evidence.example.org/decision",
        note="Synthetic fixture.",
        **(defaults | reviewed | extra),
    )


def checked(*rows, suspensions=()):
    from schema.jurisdictions import registry

    return MembershipRegister(
        description="Synthetic fixture.", memberships=rows, suspensions=suspensions
    ).check(registry())


def test_a_reviewed_membership_is_dated_states_its_basis_and_names_its_reviewer():
    assert row("sn", "aa-ecowas", date(1975, 5, 28)).holds_on(date(2026, 1, 1)) is True
    proposal = row("sn", "aa-ecowas", None, status="proposal")  # a proposal may be undated, without a basis
    assert (
        proposal.valid_from is None and proposal.basis is None and proposal.holds_on(date(2026, 1, 1)) is None
    )
    with pytest.raises(ValidationError, match="dated"):
        row("sn", "aa-ecowas", None)
    with pytest.raises(ValidationError, match="states its basis"):
        row("sn", "aa-ecowas", date(1975, 5, 28), basis=None)
    with pytest.raises(ValidationError, match="names who"):
        row("sn", "aa-ecowas", date(1975, 5, 28), reviewed_by=None)
    with pytest.raises(ValidationError, match="has not been reviewed"):
        row("sn", "aa-ecowas", None, status="proposal", reviewed_by="Someone")
    with pytest.raises(ValidationError, match="precedes"):
        row("ml", "aa-ecowas", date(2025, 1, 28), date(1975, 5, 28))
    with pytest.raises(ValidationError, match="starts with the member"):
        Membership.model_validate(
            row("sn", "aa-ecowas", date(1975, 5, 28)).model_dump() | {"id": "ml:aa-ecowas:1"}
        )
    with pytest.raises(ValidationError):  # the relationship's kind is membership, nothing else
        Membership.model_validate(row("sn", "aa-ecowas", date(1975, 5, 28)).model_dump() | {"kind": "parent"})
    with pytest.raises(ValidationError):  # a basis is one of the known kinds, with its article
        row("sn", "aa-ecowas", date(1975, 5, 28), basis=BASIS | {"kind": "custom"})
    with pytest.raises(ValidationError):
        row("sn", "aa-ecowas", date(1975, 5, 28), basis={k: v for k, v in BASIS.items() if k != "pinpoint"})


def test_an_uncertain_date_is_kept_as_written_with_its_bounds():
    left = row("mr", "aa-ecowas", "1975-05-28", "2000-12")
    assert str(left.valid_to) == "2000-12" and left.valid_to.precision == "month"
    assert left.model_dump(mode="json")["valid_to"] == "2000-12"
    assert left.holds_on(date(2000, 11, 30)) is True
    assert left.holds_on(date(2000, 12, 15)) is None  # inside the uncertain end: unknown
    assert left.holds_on(date(2001, 1, 1)) is False
    joined = row("ss", "aa-eac", "2016-08-15/2016-09-05")  # a range start
    assert joined.holds_on(date(2016, 8, 20)) is None and joined.holds_on(date(2016, 9, 5)) is True
    assert joined.holds_on(date(2016, 8, 14)) is False
    with pytest.raises(ValidationError):
        row("mr", "aa-ecowas", "1975-05-28", "2000-12?")


def test_the_register_refuses_unknown_places_non_bodies_and_overlapping_periods():
    with pytest.raises(ValueError, match="unknown jurisdiction"):
        checked(row("xx", "aa-ecowas", date(1975, 5, 28)))
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):  # a body's ID begins aa-
        row("sn", "ng", date(1975, 5, 28))
    with pytest.raises(ValueError, match="not membership"):
        checked(row("aa-eac", "aa-au", date(2001, 1, 1)))
    with pytest.raises(ValueError, match="unique"):
        checked(row("sn", "aa-ecowas", date(1975, 5, 28)), row("sn", "aa-ecowas", date(1975, 5, 28)))
    with pytest.raises(ValueError, match="overlap"):
        checked(
            row("ml", "aa-ecowas", date(1975, 5, 28), date(2025, 1, 28)),
            row("ml", "aa-ecowas", date(2025, 1, 28), n=2),
        )
    with pytest.raises(ValueError, match="overlap"):  # periods that may overlap are refused too
        checked(row("mr", "aa-ecowas", "1975-05-28", "2000-12"), row("mr", "aa-ecowas", "2000-12-20", n=2))
    # Leaving and joining again are two periods of one membership.
    checked(
        row("ml", "aa-ecowas", date(1975, 5, 28), date(2025, 1, 28)),
        row("ml", "aa-ecowas", date(2025, 1, 29), n=2),
    )


def test_the_register_cites_evidence_and_replaces_the_undated_au_arrays():
    held = register()
    au = [m for m in held.memberships if m.target_id == "aa-au"]
    assert len(au) == 55 and all(m.evidence_url.startswith("https://") and m.note for m in held.memberships)
    # A reviewed row is a dated fact with its reviewer; an undated row is a proposal.
    assert all(m.status == "proposal" for m in held.memberships if m.valid_from is None)


def test_a_regional_bodys_catalogue_row_has_no_country_code():
    assert catalogue_entry("aa-ohada").country_code is None
    assert catalogue_entry("aa-ohada").name == "Organisation for the Harmonisation of Business Law in Africa"
    assert catalogue_entry("ng").country_code == "NG" and catalogue_entry("ng-la").country_code == "NG"
    with pytest.raises(ValidationError, match="ISO country code"):
        Jurisdiction(id="gh", name="Ghana")


def view(m: Membership, suspensions=()) -> MembershipView:
    return MembershipView(
        id=m.id,
        member_id=m.subject_id,
        member_name=m.subject_id,
        body_id=m.target_id,
        body_name=m.target_id,
        valid_from=m.valid_from,
        valid_to=m.valid_to,
        basis=m.basis.model_dump(),
        evidence_url=m.evidence_url,
        note=m.note,
        reviewed_by=m.reviewed_by,
        reviewed_at=m.reviewed_at,
        suspensions=tuple(
            SuspensionView.model_validate(s.model_dump(exclude={"membership_id", "status"}))
            for s in suspensions
        ),
    )


def test_memberships_on_a_date_are_held_ended_or_unknown_never_an_empty_none():
    held = [
        view(row("ml", "aa-au", date(2001, 5, 26))),
        view(row("ml", "aa-ecowas", date(1975, 5, 28), date(2025, 1, 28))),
    ]
    on_last_day = membership_list(held, "ml", "member", date(2025, 1, 28))
    assert [m.body_id for m in on_last_day.items] == ["aa-au", "aa-ecowas"] and on_last_day.ended == ()
    after = membership_list(held, "ml", "member", date(2025, 1, 29))
    assert [m.body_id for m in after.items] == ["aa-au"]
    assert [(m.body_id, str(m.valid_to)) for m in after.ended] == [("aa-ecowas", "2025-01-28")]
    assert [m.member_id for m in membership_list(held, "aa-ecowas", "body", date(2020, 1, 1)).items] == ["ml"]
    nothing = membership_list(held, "aa-censad", "body", date(2026, 1, 1))
    assert nothing.status == "unknown" and nothing.items == () and "unknown" in nothing.notice


def test_an_uncertain_end_is_unknown_inside_its_window_and_definite_outside_it():
    held = [view(row("mr", "aa-ecowas", "1975-05-28", "2000-12"))]
    before = membership_list(held, "mr", "member", date(2000, 11, 30))
    assert (
        [m.id for m in before.items] == ["mr:aa-ecowas:1"] and before.uncertain == () and before.ended == ()
    )
    inside = membership_list(held, "mr", "member", date(2000, 12, 15))
    assert (
        inside.items == () and inside.ended == () and [m.id for m in inside.uncertain] == ["mr:aa-ecowas:1"]
    )
    after = membership_list(held, "mr", "member", date(2001, 1, 1))
    assert after.items == () and after.uncertain == () and [m.id for m in after.ended] == ["mr:aa-ecowas:1"]
    served = inside.uncertain[0].model_dump(mode="json")
    assert served["valid_to"] == "2000-12"
    assert served["valid_to_bounds"] == {
        "earliest": "2000-12-01",
        "latest": "2000-12-31",
        "precision": "month",
    }
    assert served["valid_from_bounds"]["precision"] == "day"
    assert labels(held, "mr", date(2000, 12, 15))["aa-ecowas"] == "unknown"
    assert labels(held, "mr", date(2001, 1, 1))["aa-ecowas"] == "not_held"
    assert labels(held, "mr", date(1990, 1, 1))["aa-ecowas"] == "held"


def test_a_range_start_is_unknown_inside_its_window():
    held = [view(row("ss", "aa-eac", "2016-08-15/2016-09-05"))]
    assert membership_list(held, "ss", "member", date(2016, 8, 31)).uncertain[0].id == "ss:aa-eac:1"
    assert membership_list(held, "ss", "member", date(2016, 9, 6)).items[0].id == "ss:aa-eac:1"
    early = membership_list(held, "ss", "member", date(2016, 8, 1))
    assert early.items == early.uncertain == early.ended == ()
    assert [
        labels(held, "ss", day)["aa-eac"] for day in (date(2016, 8, 1), date(2016, 8, 31), date(2016, 9, 6))
    ] == [
        "not_held",
        "unknown",
        "held",
    ]


def labels(held, member, day):
    """Each body's label in the one answer applicable law reads (api.catalogue.bodies_on)."""
    return {body: basis[0].held for body, basis in bodies_on(held, member, day).items()}


def test_which_bodies_a_country_belonged_to_is_three_valued():
    held = [
        view(row("ml", "aa-au", date(2001, 5, 26))),
        view(row("ml", "aa-ecowas", "1975-05-28", "2025-01-28")),
        view(row("ml", "aa-ohada", "1995-09-18", "1999")),
    ]
    assert labels(held, "ml", date(2026, 1, 1)) == {
        "aa-au": "held",
        "aa-ecowas": "not_held",
        "aa-ohada": "not_held",
    }
    # Inside an uncertain end, unknown, resting on that membership; a body with none reviewed is absent.
    assert labels(held, "ml", date(1999, 6, 1))["aa-ohada"] == "unknown"
    assert bodies_on(held, "ml", date(1999, 6, 1))["aa-ohada"][0].id == "ml:aa-ohada:1"
    assert "aa-censad" not in bodies_on(held, "ml", date(2026, 1, 1))


def test_a_suspension_is_context_and_never_changes_whether_a_membership_is_held():
    member = row("ga", "aa-au", date(2001, 5, 26))
    suspended = suspension("ga:aa-au:1", "2023-08-31", "2025-04-29")
    held = [view(member, (suspended,))]
    during = membership_list(held, "ga", "member", date(2024, 1, 1))
    assert [m.id for m in during.items] == ["ga:aa-au:1"] and during.uncertain == during.ended == ()
    [context] = during.items[0].suspensions
    assert context.in_effect is True and context.kind == "suspended_from_participation"
    assert context.legal_basis[0].pinpoint == "art. 2" and str(context.valid_to) == "2025-04-29"
    after = membership_list(held, "ga", "member", date(2026, 1, 1))
    assert after.items[0].suspensions[0].in_effect is False
    for day in (date(2023, 9, 1), date(2026, 1, 1)):
        assert labels(held, "ga", day)["aa-au"] == labels([view(member)], "ga", day)["aa-au"] == "held"


def test_a_suspension_is_dated_evidenced_and_overlaid_on_a_membership_of_the_register():
    member = row("ga", "aa-au", date(2001, 5, 26))
    checked(member, suspensions=(suspension("ga:aa-au:1", "2023-08-31"),))
    proposal = suspension("ga:aa-au:1", None, status="proposal", legal_basis=())
    assert proposal.valid_from is None and proposal.member_id == "ga" and proposal.body_id == "aa-au"
    with pytest.raises(ValidationError, match="dated and states its legal basis"):
        suspension("ga:aa-au:1", "2023-08-31", legal_basis=())
    with pytest.raises(ValidationError, match="decision that lifted it"):
        suspension("ga:aa-au:1", "2023-08-31", "2025-04-29", lifted_evidence_url=None)
    with pytest.raises(ValidationError, match="starts with its membership"):
        Suspension.model_validate(
            suspension("ga:aa-au:1", "2023-08-31").model_dump() | {"id": "ml:aa-au:1:s1"}
        )
    with pytest.raises(ValueError, match="no membership"):
        checked(member, suspensions=(suspension("ml:aa-au:1", "2023-08-31"),))
    with pytest.raises(ValueError, match="of a reviewed membership"):
        checked(
            row("ga", "aa-au", None, status="proposal"), suspensions=(suspension("ga:aa-au:1", "2023-08-31"),)
        )
    with pytest.raises(ValueError, match="outside its membership"):
        checked(member, suspensions=(suspension("ga:aa-au:1", "1999-01-01", "1999-12-31"),))
    with pytest.raises(ValueError, match="suspensions overlap"):
        checked(
            member,
            suspensions=(suspension("ga:aa-au:1", "2023-08-31"), suspension("ga:aa-au:1", "2024-01-01", n=2)),
        )


@pytest.fixture
def proposals(tmp_path):
    path = tmp_path / "memberships.json"
    proposal = row("sn", "aa-ecowas", None, status="proposal")
    member = row("ga", "aa-au", None, status="proposal")
    suspended = suspension("ga:aa-au:1", "2023-08-31", status="proposal")
    path.write_text(json.dumps(checked(proposal, member, suspensions=(suspended,)).model_dump(mode="json")))
    return path


def test_the_reviewer_approves_a_proposal_into_a_dated_reviewed_fact(proposals):
    with pytest.raises(Refused, match="dated"):  # a proposal without a start is reviewed with its date
        memberships.review("sn:aa-ecowas:1", "Test Reviewer", source=proposals, now=AT)
    with pytest.raises(Refused, match="states its basis"):  # and with its basis
        memberships.review(
            "sn:aa-ecowas:1", "Test Reviewer", valid_from="1975-05-28", source=proposals, now=AT
        )
    with pytest.raises(Refused, match="Name the person"):
        memberships.review("sn:aa-ecowas:1", "Your Full Name", valid_from="1975-05-28", source=proposals)
    official = BASIS | {"evidence_url": "https://www.ecowas.int/treaty/"}
    with pytest.raises(Refused, match="real https"):  # evidence is a real address, not a placeholder host
        memberships.review(
            "sn:aa-ecowas:1", "Test Reviewer", valid_from="1975-05-28", basis=official, source=proposals
        )
    with pytest.raises(Refused, match="no membership"):
        memberships.review("gh:aa-ecowas:1", "Test Reviewer", source=proposals)
    reviewed = memberships.review(
        "sn:aa-ecowas:1",
        "Test Reviewer",
        valid_from="1975-05-28",
        basis=official,
        evidence="https://www.ecowas.int/creation-history/",
        source=proposals,
        now=AT,
    )
    assert (
        reviewed.status == "reviewed"
        and reviewed.reviewed_by == "Test Reviewer"
        and reviewed.reviewed_at == AT
        and reviewed.basis.kind == "entry_into_force"
    )
    [stored] = register(proposals).reviewed()
    assert stored == reviewed
    # Run again to record its end, which may be known only to a month: the reviewer and the time are
    # those of the new review.
    later = datetime(2026, 11, 1, tzinfo=UTC)
    ended = memberships.review(
        "sn:aa-ecowas:1", "Second Reviewer", valid_to="2026-10", source=proposals, now=later
    )
    assert str(ended.valid_from) == "1975-05-28" and str(ended.valid_to) == "2026-10"
    assert ended.reviewed_by == "Second Reviewer" and ended.reviewed_at == later
    reopened = memberships.review(
        "sn:aa-ecowas:1", "Second Reviewer", ongoing=True, source=proposals, now=later
    )
    assert reopened.valid_to is None


def test_the_reviewer_approves_a_suspension_of_a_reviewed_membership(proposals):
    official = BASIS | {"evidence_url": "https://au.int/treaty/"}
    with pytest.raises(Refused, match="real https"):  # the decision and the legal basis are real addresses
        memberships.review("ga:aa-au:1:s1", "Test Reviewer", source=proposals, now=AT)
    data = json.loads(proposals.read_text())
    data["suspensions"][0] |= {
        "evidence_url": "https://www.peaceau.org/decision.pdf",
        "legal_basis": [
            {"instrument": "Act", "pinpoint": "art. 30", "evidence_url": "https://au.int/act.pdf"}
        ],
    }
    proposals.write_text(json.dumps(data))
    with pytest.raises(Refused, match="of a reviewed membership"):
        memberships.review("ga:aa-au:1:s1", "Test Reviewer", source=proposals, now=AT)
    memberships.review(
        "ga:aa-au:1",
        "Test Reviewer",
        valid_from="2001-05-26",
        basis=official,
        evidence="https://au.int/members/",
        source=proposals,
    )
    with pytest.raises(Refused, match="legal basis is recorded in its proposal"):
        memberships.review("ga:aa-au:1:s1", "Test Reviewer", basis=official, source=proposals)
    with pytest.raises(Refused, match="Only a suspension"):
        memberships.review(
            "ga:aa-au:1", "Test Reviewer", lifted_evidence="https://au.int/x", source=proposals
        )
    with pytest.raises(Refused, match="no suspension"):
        memberships.review("ga:aa-au:1:s2", "Test Reviewer", source=proposals)
    reviewed = memberships.review("ga:aa-au:1:s1", "Test Reviewer", source=proposals, now=AT)
    assert reviewed.status == "reviewed" and reviewed.valid_to is None
    with pytest.raises(Refused, match="decision that lifted it"):
        memberships.review("ga:aa-au:1:s1", "Test Reviewer", valid_to="2025-04-29", source=proposals, now=AT)
    lifted = memberships.review(
        "ga:aa-au:1:s1",
        "Test Reviewer",
        valid_to="2025-04-29",
        lifted_evidence="https://www.peaceau.org/lifted.pdf",
        source=proposals,
        now=AT,
    )
    assert str(lifted.valid_to) == "2025-04-29" and register(proposals).reviewed_suspensions() == (lifted,)


def researched(body):
    return {m.subject_id: m for m in register().memberships if m.target_id == body}


def test_au_proposals_start_at_entry_into_force_a_later_deposit_or_admission_and_stay_proposals():
    au = researched("aa-au")
    assert all(
        m.status == "proposal" and m.basis and m.basis.evidence_url.startswith("https://")
        for m in au.values()
    )
    for member, m in au.items():
        if m.basis.kind == "entry_into_force":
            assert str(m.valid_from) == "2001-05-26" and m.basis.pinpoint == "art. 28", member
        elif m.basis.kind == "deposit":  # an OAU member that deposited after the Act entered into force
            assert m.valid_from.earliest > date(2001, 5, 26) and f"deposit {m.valid_from}" in m.note, member
    assert (au["ma"].basis.kind, str(au["ma"].valid_from)) == ("admission_decision", "2017-01-30")
    # South Sudan's admission decision is known only to a window: the majority's receipt or its announcement.
    assert (au["ss"].basis.kind, au["ss"].valid_from.precision) == ("admission_decision", "range")
    assert str(au["dz"].valid_from) == "2001-05-31" and au["dz"].basis.kind == "deposit"
    assert sum(m.basis.kind == "entry_into_force" for m in au.values()) == 38


def test_ecowas_founders_start_on_signature_under_article_62_and_mauritania_left_in_december_2000():
    ecowas = researched("aa-ecowas")
    founders = [m for m in ecowas.values() if m.basis.kind == "provisional_application_on_signature"]
    assert len(founders) == 15 and all(str(m.valid_from) == "1975-05-28" for m in founders)
    assert all(m.basis.pinpoint == "art. 62(1)" and "provisionally" in m.basis.quote for m in founders)
    mauritania = ecowas["mr"]
    assert mauritania.status == "proposal" and str(mauritania.valid_to) == "2000-12"
    assert mauritania.holds_on(date(2000, 12, 15)) is None and mauritania.holds_on(date(2001, 1, 1)) is False
    assert str(ecowas["cv"].valid_from) == "1976/1977" and ecowas["cv"].basis.pinpoint == "art. 62(2)"
    assert all(m.status == "proposal" for m in ecowas.values())


def test_a_basis_is_set_only_where_its_source_states_it():
    eac, ohada = researched("aa-eac"), researched("aa-ohada")
    assert str(eac["ss"].valid_from) == "2016-08-15/2016-09-05" and eac["ss"].basis is None
    assert {k for k, m in eac.items() if m.basis} == {"ke", "tz", "ug", "cd"}
    assert all(m.basis.kind == "entry_into_force" for m in ohada.values() if m.valid_from is not None)
    assert all(m.basis is None for m in ohada.values() if m.valid_from is None)
    for body in ("aa-uemoa", "aa-cemac"):
        assert all(m.basis is None and "Basis not evidenced" in m.note for m in researched(body).values())
    # Every row without a basis says why in its note.
    assert all(m.basis or "basis" in m.note.lower() for m in register().memberships)
