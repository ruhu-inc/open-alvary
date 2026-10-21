"""Membership of the African Union and the regional bodies (JUR-01, JUR-04): dated, evidenced relationships
that become facts only when a reviewer approves them; a proposal is never a fact. The examples are
synthetic: their dates and evidence are fixtures, not research."""

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from api.catalogue import membership_list
from operations import memberships
from operations.publish import Refused
from schema.jurisdictions import catalogue_entry
from schema.memberships import Membership, MembershipRegister, register
from schema.models import Jurisdiction
from schema.public import MembershipView

AT = datetime(2026, 10, 21, 9, tzinfo=UTC)


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
        **(reviewed | extra),
    )


def checked(*rows):
    from schema.jurisdictions import registry

    return MembershipRegister(description="Synthetic fixture.", memberships=rows).check(registry())


def test_a_reviewed_membership_is_dated_and_names_its_reviewer_and_a_proposal_names_none():
    assert row("sn", "aa-ecowas", date(1975, 5, 28)).holds_on(date(2026, 1, 1))
    assert row("sn", "aa-ecowas", None, status="proposal").valid_from is None  # a proposal may be undated
    with pytest.raises(ValidationError, match="dated"):
        row("sn", "aa-ecowas", None)
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


def view(m: Membership) -> MembershipView:
    return MembershipView(
        id=m.id,
        member_id=m.subject_id,
        member_name=m.subject_id,
        body_id=m.target_id,
        body_name=m.target_id,
        valid_from=m.valid_from,
        valid_to=m.valid_to,
        evidence_url=m.evidence_url,
        note=m.note,
        reviewed_by=m.reviewed_by,
        reviewed_at=m.reviewed_at,
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
    assert [(m.body_id, m.valid_to) for m in after.ended] == [("aa-ecowas", date(2025, 1, 28))]
    assert [m.member_id for m in membership_list(held, "aa-ecowas", "body", date(2020, 1, 1)).items] == ["ml"]
    nothing = membership_list(held, "aa-censad", "body", date(2026, 1, 1))
    assert nothing.status == "unknown" and nothing.items == () and "unknown" in nothing.notice


@pytest.fixture
def proposals(tmp_path):
    path = tmp_path / "memberships.json"
    proposal = row("sn", "aa-ecowas", None, status="proposal")
    path.write_text(json.dumps(checked(proposal).model_dump(mode="json")))
    return path


def test_the_reviewer_approves_a_proposal_into_a_dated_reviewed_fact(proposals):
    with pytest.raises(Refused, match="dated"):  # a proposal without a start is reviewed with its date
        memberships.review("sn:aa-ecowas:1", "Test Reviewer", source=proposals, now=AT)
    with pytest.raises(Refused, match="Name the person"):
        memberships.review("sn:aa-ecowas:1", "Your Full Name", valid_from=date(1975, 5, 28), source=proposals)
    with pytest.raises(Refused, match="real https"):  # evidence is a real address, not a placeholder host
        memberships.review("sn:aa-ecowas:1", "Test Reviewer", valid_from=date(1975, 5, 28), source=proposals)
    with pytest.raises(Refused, match="no membership"):
        memberships.review("gh:aa-ecowas:1", "Test Reviewer", source=proposals)
    reviewed = memberships.review(
        "sn:aa-ecowas:1",
        "Test Reviewer",
        valid_from=date(1975, 5, 28),
        evidence="https://www.ecowas.int/creation-history/",
        source=proposals,
        now=AT,
    )
    assert (
        reviewed.status == "reviewed"
        and reviewed.reviewed_by == "Test Reviewer"
        and reviewed.reviewed_at == AT
    )
    [stored] = register(proposals).reviewed()
    assert stored == reviewed
    # Run again to record its end: the reviewer and the time are those of the new review.
    later = datetime(2026, 11, 1, tzinfo=UTC)
    ended = memberships.review(
        "sn:aa-ecowas:1", "Second Reviewer", valid_to=date(2026, 10, 31), source=proposals, now=later
    )
    assert ended.valid_from == date(1975, 5, 28) and ended.valid_to == date(2026, 10, 31)
    assert ended.reviewed_by == "Second Reviewer" and ended.reviewed_at == later
    reopened = memberships.review(
        "sn:aa-ecowas:1", "Second Reviewer", ongoing=True, source=proposals, now=later
    )
    assert reopened.valid_to is None
