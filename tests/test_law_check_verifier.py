"""Unit and integration tests for the deterministic Law check verifier, receipt schema, and ES256 signer."""

import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from api.catalogue import LegacyCatalogue
from api.receipts import (
    ReceiptSigner,
    canonical_json,
    claim_digest,
    load_jwks_from_env,
    load_signer_from_env,
    verify_receipt,
)
from api.repositories import LegacyRepository
from retrieval.verify import (
    InvalidClaimArguments,
    VerifyToolError,
    match_quote,
    normalize_quote,
    verify_claim,
)
from schema.models import (
    Citation,
    Corpus,
    Language,
    RightsRecord,
    Source,
    SourceVersion,
    StructureNode,
)
from schema.public import CheckClaimResponse, Currentness, Receipt

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
TODAY = date(2026, 11, 22)


def _generate_es256_pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture
def signer() -> ReceiptSigner:
    return ReceiptSigner(
        private_pem=_generate_es256_pem(),
        kid="oa-receipt-2026-12",
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2027-12-31T23:59:59Z",
    )


# --- 1. Canonical JSON, claim_digest, ReceiptSigner, verify_receipt ---------------------------------


def test_canonical_json_rfc8785_and_float_rejection():
    assert canonical_json({"b": 1, "a": [True, False, None, "hi"]}) == b'{"a":[true,false,null,"hi"],"b":1}'
    # UTF-16 code-unit key order: U+E000 (\ue000) sorts AFTER U+1F600 (\ud83d\ude00) in UTF-16
    assert canonical_json({"\ue000": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"\ue000":1}'.encode("utf-8")
    with pytest.raises(ValueError, match="floating-point"):
        canonical_json({"score": 1.5})


def test_claim_digest_only_checked_fields_and_excludes_reading():
    c1 = {"citation": "FOIA 2011, s. 1(1)", "quote": "Notwithstanding anything", "jurisdiction": "ng"}
    c2 = {**c1, "reading": "This means anyone can request public records."}
    c3 = {**c1, "as_of": None, "language": None}
    assert len(claim_digest(c1)) == 64
    assert claim_digest(c1) == claim_digest(c2)
    assert claim_digest(c1) == claim_digest(c3)
    assert claim_digest({**c1, "as_of": date(2025, 1, 1)}) == claim_digest({**c1, "as_of": "2025-01-01"})
    assert claim_digest(c1) != claim_digest({**c1, "jurisdiction": "rw"})


def test_signer_and_verify_receipt_lifecycle(signer: ReceiptSigner):
    claim = {"citation": "FOIA 2011, s. 1(1)", "quote": "Some exact quoted text from the Act here."}
    receipt_body = {
        "schema_version": "oa-receipt-1",
        "receipt_id": "rcp_01TEST00000000000000000000",
        "issued_at": "2026-11-20T10:00:00Z",
        "key": {"alg": "ES256", "kid": signer.kid},
        "mechanism": {"name": "oa-verify", "version": "1.0"},
        "dataset": "unknown",
        "claim": {
            "digest": claim_digest(claim),
            "assessed_at": "2026-11-20",
            "date_sent": False,
            "jurisdiction": None,
        },
        "part": None,
        "nearest": None,
        "alternatives": [],
        "findings": {
            "quote": None,
            "matched_text": None,
            "opening_text": None,
            "context_before": None,
            "context_after": None,
            "omitted": [],
            "currentness": None,
            "applicability": None,
            "newer_text": None,
        },
        "status": "not_in_catalogue",
        "statement": "Open Alvary's catalogue does not hold a public record matching this citation.",
    }
    sig = signer.sign(receipt_body)
    assert "=" not in sig
    receipt = {**receipt_body, "signature": sig}
    jwks = signer.jwks()

    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
    res = verify_receipt(receipt, jwks, claim=claim, now=now)
    assert res == {"ok": True, "status": "not_in_catalogue", "reason": None, "age_days": 5}

    # Tampered payload fails
    tampered = {**receipt, "status": "matches"}
    assert verify_receipt(tampered, jwks, now=now)["ok"] is False
    assert verify_receipt(tampered, jwks, now=now)["reason"] == "invalid_signature"

    # Wrong claim digest fails
    wrong_claim = verify_receipt(receipt, jwks, claim={"citation": "Other Act, s. 1"}, now=now)
    assert wrong_claim["ok"] is False and wrong_claim["reason"] == "claim_digest_mismatch"

    # Reading does not break claim digest
    assert verify_receipt(receipt, jwks, claim={**claim, "reading": "AI summary"}, now=now)["ok"] is True

    # Unknown kid fails
    assert verify_receipt(receipt, {"keys": []}, now=now)["reason"] == "unknown_kid"

    # Revoked key fails
    revoked_jwks = {"keys": [{**jwks["keys"][0], "revoked_at": "2026-11-21T00:00:00Z"}]}
    assert verify_receipt(receipt, revoked_jwks, now=now)["reason"] == "key_revoked"

    # Retired key (valid_to in the past relative to now, but issued_at <= valid_to) STILL verifies!
    retired_jwks = {"keys": [{**jwks["keys"][0], "valid_to": "2026-11-21T00:00:00Z"}]}
    assert verify_receipt(receipt, retired_jwks, now=now)["ok"] is True

    # Issued after valid_to fails
    expired_jwks = {"keys": [{**jwks["keys"][0], "valid_to": "2026-11-19T00:00:00Z"}]}
    assert verify_receipt(receipt, expired_jwks, now=now)["reason"] == "issued_after_key_valid"

    # Issued before valid_from fails
    future_jwks = {"keys": [{**jwks["keys"][0], "valid_from": "2026-11-21T00:00:00Z"}]}
    assert verify_receipt(receipt, future_jwks, now=now)["reason"] == "issued_before_key_valid"


def test_env_loaders(monkeypatch):
    monkeypatch.delenv("ALVARY_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ALVARY_RECEIPT_KEYS_JSON", raising=False)
    assert load_signer_from_env() is None
    assert load_jwks_from_env() == {"keys": []}

    pem = _generate_es256_pem().decode("utf-8")
    monkeypatch.setenv("ALVARY_RECEIPT_SIGNING_KEY", pem)
    monkeypatch.setenv("ALVARY_RECEIPT_KID", "oa-test-kid")
    monkeypatch.setenv("ALVARY_RECEIPT_VALID_FROM", "2026-01-01T00:00:00Z")
    loaded = load_signer_from_env()
    assert loaded is not None and loaded.kid == "oa-test-kid"
    jwks = load_jwks_from_env()
    assert len(jwks["keys"]) == 1 and jwks["keys"][0]["kid"] == "oa-test-kid"

    extra = {"keys": [{"kid": "old-kid", "kty": "EC", "crv": "P-256"}]}
    monkeypatch.setenv("ALVARY_RECEIPT_KEYS_JSON", json.dumps(extra))
    assert load_jwks_from_env() == extra


# --- 2. Quote normalisation & matching (§8 Step 3) --------------------------------------------------


def test_normalize_quote_rules():
    raw = "  \u201cOf\ufb01ce\u00ad\u200b\u00a0\u2014\u00a0‘test’\u201d\n\t\ufb00 \ufb02 \ufb03 \ufb04 \u2212 \u2013 \u2011  "
    assert normalize_quote(raw) == "\"Office - 'test'\" ff fl ffi ffl - - -"
    # Case is preserved
    assert normalize_quote("Section One") == "Section One"
    # Hyphenated line-end is NOT joined
    assert normalize_quote("infor-\nmation") == "infor- mation"


def test_match_quote_exact_normalised_absent_and_boundaries():
    part = (
        "(1) Notwithstanding anything contained in any other Act, Law or Regulation, "
        "the right of any person to access or request information\u2014whether \u201cofficial\u201d "
        "or \ufb01led\u2014is hereby established. No impersonate shall qualify."
    )
    # Exact match
    q_exact = "the right of any person to access or request information"
    res = match_quote(part, q_exact)
    assert res["quote"] == "exact"
    assert res["matched_text"] == q_exact
    assert res["opening_text"] is None
    assert "Regulation, " in res["context_before"]
    assert res["context_after"].startswith("\u2014whether")

    # Normalised match (straight quotes, hyphen, expanded ligature) maps back to original slice!
    q_norm = 'request information-whether "official" or filed-is hereby established.'
    res_norm = match_quote(part, q_norm)
    assert res_norm["quote"] == "normalised"
    assert (
        res_norm["matched_text"]
        == "request information\u2014whether \u201cofficial\u201d or \ufb01led\u2014is hereby established."
    )

    # Punctuation-safe word boundary: starting with (1) and ending with . works
    q_punct = "(1) Notwithstanding anything contained in any other Act, Law or Regulation,"
    assert match_quote(part, q_punct)["quote"] == "exact"

    # Mid-word match rejected: "person" inside "impersonate"
    mid_word = "is hereby established. No person shall qualify."
    assert match_quote(part, mid_word)["quote"] == "absent"
    assert match_quote(part, mid_word)["opening_text"] == part

    # Absent with >400 char part trims opening_text cleanly on word boundary
    long_part = ("alpha beta gamma delta " * 30).strip()
    absent = match_quote(long_part, "this wording definitely does not appear anywhere in the section")
    assert absent["quote"] == "absent"
    assert absent["matched_text"] is None
    assert len(absent["opening_text"]) <= 400
    assert not absent["opening_text"].endswith(" ")


def test_match_quote_length_whole_part_and_ellipses():
    short_part = "Short section text."
    # Whole part's words allowed even when < 40 chars
    assert match_quote(short_part, "Short section text.")["quote"] == "exact"
    # Shorter than 40 chars and NOT whole part -> quote_too_short
    with pytest.raises(VerifyToolError) as exc:
        match_quote(short_part, "Short section")
    assert exc.value.code == "quote_too_short"

    # Empty after stripping ellipses -> ValueError
    with pytest.raises(ValueError):
        match_quote(short_part, "  ... …  ")

    # with_omissions
    part = (
        "Section 1. Every citizen of Nigeria has the right to request access to public records "
        "held by any public institution, subject to the exemptions stated in this Act, "
        "within seven days of receiving the application."
    )
    q_omit = "Every citizen of Nigeria has the right ... within seven days of receiving the application."
    res = match_quote(part, q_omit)
    assert res["quote"] == "with_omissions"
    assert res["matched_text"].startswith("Every citizen")
    assert res["matched_text"].endswith("receiving the application.")
    assert len(res["omitted"]) == 1
    s, e = res["omitted"][0]
    assert "public institution" in res["matched_text"][s:e]

    # Piece < 12 chars -> quote_too_short
    with pytest.raises(VerifyToolError) as exc2:
        match_quote(part, "Every ... within seven days of receiving the application.")
    assert exc2.value.code == "quote_too_short"

    # Spread > 4000 chars -> quote_too_spread
    huge_part = (
        "Opening piece of sufficient length here. "
        + ("x " * 2500)
        + "Closing piece of sufficient length here."
    )
    with pytest.raises(VerifyToolError) as exc3:
        match_quote(
            huge_part, "Opening piece of sufficient length here. ... Closing piece of sufficient length here."
        )
    assert exc3.value.code == "quote_too_spread"


# --- 3. Full verify_claim tests against LegacyCatalogue ---------------------------------------------


def _make_rights(
    source_id: str, hashes: list[str], *, status: str = "OPEN", metadata_public: bool = True
) -> RightsRecord:
    return RightsRecord(
        id=f"rights-{source_id}",
        source_id=source_id,
        status=status,
        rights_holder="Federal Republic of Nigeria",
        licence="Public Domain",
        redistribution_allowed="yes",
        commercial_reuse_allowed="yes",
        derivatives_allowed="yes",
        attribution_required=False,
        attribution_text=None,
        privacy_status="cleared",
        reviewer="test-reviewer",
        reviewed_at=NOW,
        evidence=["https://example.org/evidence"],
        legal_note="Test fixture",
        database_rights_notes="Test fixture",
        approved_content_hashes=hashes,
        metadata_public=metadata_public,
        metadata_basis="Public legislation",
    )


def _build_test_catalogue(store, base_catalogue: Corpus) -> LegacyCatalogue:
    """Build a rich SQLite catalogue exercising all 13 verifier statuses."""
    corpus = Corpus(
        jurisdictions=base_catalogue.jurisdictions,
        authorities=base_catalogue.authorities,
        languages=[
            *base_catalogue.languages,
            *(
                []
                if any(lang.id == "fr" for lang in base_catalogue.languages)
                else [Language(id="fr", name="French", tag="fr")]
            ),
        ],
    )
    auth_id = base_catalogue.authorities[0].id

    # 1. Main current Act with s. 1 and s. 1(1)
    foi_text = (
        "1. Right of access to records.\n"
        "(1) Notwithstanding anything contained in any other Act, Law or Regulation, "
        "the right of any person to access or request information is hereby established.\n"
        "(2) An applicant need not demonstrate any specific interest in the information."
    )
    foi_bytes = foi_text.encode("utf-8")
    foi_hash = sha256(foi_bytes).hexdigest()
    s1_sub1_start = foi_bytes.index(b"(1)")
    s1_sub1_end = foi_bytes.index(b"(2)") - 1

    corpus.sources.append(
        Source(
            id="ng-foi-act-2011",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Freedom of Information Act 2011",
            citation="Freedom of Information Act 2011",
            year=2011,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/foia-2011",
        )
    )
    corpus.versions.append(
        SourceVersion(
            id="ver-foi-1",
            source_id="ng-foi-act-2011",
            canonical_url="https://official.example.ng/foia-2011",
            retrieved_at=NOW,
            raw_hash=foi_hash,
            content_hash=foi_hash,
            canonical_text=foi_text,
            parser="fixture",
            language="en",
            effective_from=date(2011, 5, 28),
            verification_status="verified",
            content_verifier="reviewer",
            content_verified_at=NOW,
        )
    )
    corpus.structure.extend(
        [
            StructureNode(
                id="anc-foi-s1",
                source_id="ng-foi-act-2011",
                version_id="ver-foi-1",
                kind="section",
                locator="sec_1",
                label="1",
                heading="Right of access to records.",
                order=0,
                start_byte=0,
                end_byte=len(foi_bytes),
                source_hash=foi_hash,
            ),
            StructureNode(
                id="anc-foi-s1-1",
                source_id="ng-foi-act-2011",
                version_id="ver-foi-1",
                parent_id="anc-foi-s1",
                kind="subsection",
                locator="sec_1__subsec_1",
                label="(1)",
                order=1,
                start_byte=s1_sub1_start,
                end_byte=s1_sub1_end,
                source_hash=foi_hash,
            ),
        ]
    )
    corpus.rights.append(_make_rights("ng-foi-act-2011", [foi_hash]))

    # 2. Metadata-only Act (no public text -> Row 3 text_not_available)
    corpus.sources.append(
        Source(
            id="ng-meta-only-2020",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Metadata Only Act 2020",
            citation="Metadata Only Act 2020",
            year=2020,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/meta-2020",
        )
    )
    corpus.rights.append(_make_rights("ng-meta-only-2020", [], status="LINK_ONLY"))

    # 2b. Whole-record restricted/withdrawn Act (not public at all -> Row 1 not_in_catalogue)
    corpus.sources.append(
        Source(
            id="ng-withdrawn-2019",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Withdrawn Record Act 2019",
            citation="Withdrawn Record Act 2019",
            year=2019,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/withdrawn-2019",
        )
    )
    corpus.rights.append(_make_rights("ng-withdrawn-2019", [], status="RESTRICTED"))

    # 3. Repealed Act (Row 7 not_in_force)
    rep_text = "1. This repealed provision formerly governed transitional licensing across the federation."
    rep_hash = sha256(rep_text.encode()).hexdigest()
    corpus.sources.append(
        Source(
            id="ng-repealed-1999",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Repealed Licensing Act 1999",
            citation="Repealed Licensing Act 1999",
            year=1999,
            language="en",
            legal_status="repealed",
            canonical_url="https://official.example.ng/repealed-1999",
        )
    )
    corpus.versions.append(
        SourceVersion(
            id="ver-rep-1",
            source_id="ng-repealed-1999",
            canonical_url="https://official.example.ng/repealed-1999",
            retrieved_at=NOW,
            raw_hash=rep_hash,
            content_hash=rep_hash,
            canonical_text=rep_text,
            parser="fixture",
            language="en",
            effective_from=date(1999, 1, 1),
            effective_until=date(2020, 1, 1),
            verification_status="verified",
            content_verifier="reviewer",
            content_verified_at=NOW,
        )
    )
    corpus.structure.append(
        StructureNode(
            id="anc-rep-s1",
            source_id="ng-repealed-1999",
            version_id="ver-rep-1",
            kind="section",
            locator="sec_1",
            label="1",
            order=0,
            start_byte=0,
            end_byte=len(rep_text.encode()),
            source_hash=rep_hash,
        )
    )
    corpus.rights.append(_make_rights("ng-repealed-1999", [rep_hash]))

    # 4. Two works sharing an alias ("Shared Act") where only ONE has public text (Row 2a -> ambiguous!)
    shared_text = "1. Shared Act provision text of sufficient length for checking."
    shared_hash = sha256(shared_text.encode()).hexdigest()
    for idx, has_text in ((1, True), (2, False)):
        sid = f"ng-shared-{idx}"
        corpus.sources.append(
            Source(
                id=sid,
                jurisdiction_id="ng",
                authority_id=auth_id,
                document_type="act",
                title=f"Shared Act Variant {idx}",
                citation=f"Shared Act Variant {idx}",
                year=2010 + idx,
                language="en",
                legal_status="current",
                canonical_url=f"https://official.example.ng/shared-{idx}",
            )
        )
        corpus.citations.append(Citation(id=f"cit-shared-{idx}", source_id=sid, value="Shared Act"))
        if has_text:
            corpus.versions.append(
                SourceVersion(
                    id=f"ver-shared-{idx}",
                    source_id=sid,
                    canonical_url=f"https://official.example.ng/shared-{idx}",
                    retrieved_at=NOW,
                    raw_hash=shared_hash,
                    content_hash=shared_hash,
                    canonical_text=shared_text,
                    parser="fixture",
                    language="en",
                    verification_status="verified",
                    content_verifier="reviewer",
                    content_verified_at=NOW,
                )
            )
            corpus.structure.append(
                StructureNode(
                    id=f"anc-shared-{idx}-s1",
                    source_id=sid,
                    version_id=f"ver-shared-{idx}",
                    kind="section",
                    locator="sec_1",
                    label="1",
                    order=0,
                    start_byte=0,
                    end_byte=len(shared_text.encode()),
                    source_hash=shared_hash,
                )
            )
            corpus.rights.append(_make_rights(sid, [shared_hash]))
        else:
            corpus.rights.append(_make_rights(sid, [], status="LINK_ONLY"))

    # 5. Act with two dated texts (several_texts + as_of testing)
    v1_text = "1. Original 2015 wording of the Multi Version Act section one for testing."
    v2_text = "1. Amended 2022 wording of the Multi Version Act section one for testing."
    v1_hash, v2_hash = sha256(v1_text.encode()).hexdigest(), sha256(v2_text.encode()).hexdigest()
    corpus.sources.append(
        Source(
            id="ng-multi-ver",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Multi Version Act",
            citation="Multi Version Act",
            year=2015,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/multi",
        )
    )
    corpus.versions.extend(
        [
            SourceVersion(
                id="ver-multi-1",
                source_id="ng-multi-ver",
                canonical_url="https://official.example.ng/multi-2015",
                retrieved_at=NOW,
                raw_hash=v1_hash,
                content_hash=v1_hash,
                canonical_text=v1_text,
                parser="fixture",
                language="en",
                effective_from=date(2015, 1, 1),
                effective_until=date(2022, 1, 1),
                verification_status="verified",
                content_verifier="reviewer",
                content_verified_at=NOW,
            ),
            SourceVersion(
                id="ver-multi-2",
                source_id="ng-multi-ver",
                canonical_url="https://official.example.ng/multi-2022",
                retrieved_at=NOW,
                raw_hash=v2_hash,
                content_hash=v2_hash,
                canonical_text=v2_text,
                parser="fixture",
                language="en",
                effective_from=date(2022, 1, 1),
                verification_status="verified",
                content_verifier="reviewer",
                content_verified_at=NOW,
            ),
        ]
    )
    corpus.structure.extend(
        [
            StructureNode(
                id="anc-multi-v1-s1",
                source_id="ng-multi-ver",
                version_id="ver-multi-1",
                kind="section",
                locator="sec_1",
                label="1",
                order=0,
                start_byte=0,
                end_byte=len(v1_text.encode()),
                source_hash=v1_hash,
            ),
            StructureNode(
                id="anc-multi-v2-s1",
                source_id="ng-multi-ver",
                version_id="ver-multi-2",
                kind="section",
                locator="sec_1",
                label="1",
                order=0,
                start_byte=0,
                end_byte=len(v2_text.encode()),
                source_hash=v2_hash,
            ),
        ]
    )
    corpus.rights.append(_make_rights("ng-multi-ver", [v1_hash, v2_hash]))

    # 5b. One Act published in two languages (as Rwanda's are in three): the same section in each text
    en_text = "1. Every person is equal before the law and enjoys equal protection of the law."
    fr_text = "1. Toute personne est égale devant la loi et jouit d'une égale protection de la loi."
    en_hash, fr_hash = sha256(en_text.encode()).hexdigest(), sha256(fr_text.encode()).hexdigest()
    corpus.sources.append(
        Source(
            id="ng-two-lang",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Two Languages Act",
            citation="Two Languages Act",
            year=2020,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/two-lang",
        )
    )
    for vid, lang, text_, digest in (
        ("ver-lang-en", "en", en_text, en_hash),
        ("ver-lang-fr", "fr", fr_text, fr_hash),
    ):
        corpus.versions.append(
            SourceVersion(
                id=vid,
                source_id="ng-two-lang",
                canonical_url=f"https://official.example.ng/two-lang-{lang}",
                retrieved_at=NOW,
                raw_hash=digest,
                content_hash=digest,
                canonical_text=text_,
                parser="fixture",
                language=lang,
                verification_status="verified",
                content_verifier="reviewer",
                content_verified_at=NOW,
            )
        )
        corpus.structure.append(
            StructureNode(
                id=f"anc-lang-{lang}-s1",
                source_id="ng-two-lang",
                version_id=vid,
                kind="section",
                locator="sec_1",
                label="1",
                order=0,
                start_byte=0,
                end_byte=len(text_.encode()),
                source_hash=digest,
            )
        )
    corpus.rights.append(_make_rights("ng-two-lang", [en_hash, fr_hash]))

    # 6. Replaced text (old version not approved; newer version has s. 1 but NOT s. 2)
    old_text = "1. Old wording of section one in replaced text.\n2. Section two that was deleted."
    new_text = "1. Corrected wording of section one in the newest public text of the Act."
    old_hash, new_hash = sha256(old_text.encode()).hexdigest(), sha256(new_text.encode()).hexdigest()
    old_b = old_text.encode()
    s2_start = old_b.index(b"2.")
    corpus.sources.append(
        Source(
            id="ng-replaced-act",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Replaced Reading Act 2021",
            citation="Replaced Reading Act 2021",
            year=2021,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/replaced-act",
        )
    )
    corpus.versions.extend(
        [
            SourceVersion(
                id="ver-repl-old",
                source_id="ng-replaced-act",
                canonical_url="https://official.example.ng/replaced-act-old",
                retrieved_at=NOW,
                raw_hash=old_hash,
                content_hash=old_hash,
                canonical_text=old_text,
                parser="fixture",
                language="en",
                verification_status="verified",
                content_verifier="reviewer",
                content_verified_at=NOW,
            ),
            SourceVersion(
                id="ver-repl-new",
                source_id="ng-replaced-act",
                prior_version_id="ver-repl-old",
                canonical_url="https://official.example.ng/replaced-act-new",
                retrieved_at=NOW,
                raw_hash=new_hash,
                content_hash=new_hash,
                canonical_text=new_text,
                parser="fixture",
                language="en",
                verification_status="verified",
                content_verifier="reviewer",
                content_verified_at=NOW,
            ),
        ]
    )
    corpus.structure.extend(
        [
            StructureNode(
                id="anc-repl-old-s1",
                source_id="ng-replaced-act",
                version_id="ver-repl-old",
                kind="section",
                locator="sec_1",
                label="1",
                order=0,
                start_byte=0,
                end_byte=s2_start - 1,
                source_hash=old_hash,
            ),
            StructureNode(
                id="anc-repl-old-s2",
                source_id="ng-replaced-act",
                version_id="ver-repl-old",
                kind="section",
                locator="sec_2",
                label="2",
                order=1,
                start_byte=s2_start,
                end_byte=len(old_b),
                source_hash=old_hash,
            ),
            StructureNode(
                id="anc-repl-new-s1",
                source_id="ng-replaced-act",
                version_id="ver-repl-new",
                kind="section",
                locator="sec_1",
                label="1",
                order=0,
                start_byte=0,
                end_byte=len(new_text.encode()),
                source_hash=new_hash,
            ),
        ]
    )
    # Only new_hash is approved -> ver-repl-old is replaced (not public)!
    corpus.rights.append(_make_rights("ng-replaced-act", [new_hash]))

    # 7. Two texts of one Act whose s. 1 has the very same words (the Nigeria Tax Act's two readings)
    same_s1 = "1. This Act may be cited as the Two Readings Act and applies throughout the federation."
    readings = {
        "ver-two-1": (same_s1 + "\n2. The first reading's section two.", date(2015, 1, 1), date(2020, 1, 1)),
        "ver-two-2": (same_s1 + "\n2. The second reading's section two, amended.", date(2020, 1, 1), None),
    }
    corpus.sources.append(
        Source(
            id="ng-two-readings",
            jurisdiction_id="ng",
            authority_id=auth_id,
            document_type="act",
            title="Two Readings Act",
            citation="Two Readings Act",
            year=2015,
            language="en",
            legal_status="current",
            canonical_url="https://official.example.ng/two-readings",
        )
    )
    hashes = []
    for vid, (text, start, until) in readings.items():
        digest = sha256(text.encode()).hexdigest()
        hashes.append(digest)
        corpus.versions.append(
            SourceVersion(
                id=vid,
                source_id="ng-two-readings",
                canonical_url=f"https://official.example.ng/two-readings/{vid}",
                retrieved_at=NOW,
                raw_hash=digest,
                content_hash=digest,
                canonical_text=text,
                parser="fixture",
                language="en",
                effective_from=start,
                effective_until=until,
                verification_status="verified",
                content_verifier="reviewer",
                content_verified_at=NOW,
            )
        )
        s2_at = text.encode().index(b"\n2.")
        corpus.structure.extend(
            [
                StructureNode(
                    id=f"anc-{vid}-s1",
                    source_id="ng-two-readings",
                    version_id=vid,
                    kind="section",
                    locator="sec_1",
                    label="1",
                    order=0,
                    start_byte=0,
                    end_byte=s2_at,
                    source_hash=digest,
                ),
                StructureNode(
                    id=f"anc-{vid}-s2",
                    source_id="ng-two-readings",
                    version_id=vid,
                    kind="section",
                    locator="sec_2",
                    label="2",
                    order=1,
                    start_byte=s2_at + 1,
                    end_byte=len(text.encode()),
                    source_hash=digest,
                ),
            ]
        )
    corpus.rights.append(_make_rights("ng-two-readings", hashes))

    store.save(corpus)
    conn = store.engine.connect()
    return LegacyCatalogue(LegacyRepository(conn))


@pytest.fixture
def cat(store, catalogue):
    c = _build_test_catalogue(store, catalogue)
    yield c
    c.repository.connection.close()


def _assert_valid_response(resp: dict, signer: ReceiptSigner) -> Receipt:
    CheckClaimResponse.model_validate(resp)
    receipt = Receipt.model_validate(resp["receipt"])
    v = verify_receipt(resp["receipt"], signer.jwks(), claim=resp["claim"])
    assert v["ok"] is True, f"Receipt failed verification: {v}"
    assert v["status"] == receipt.status
    return receipt


def test_input_validation_and_missing_signer(cat: LegacyCatalogue, signer: ReceiptSigner):
    with pytest.raises(ValueError, match="citation or anchor_id"):
        verify_claim(cat, {}, signer=signer, today=TODAY)
    with pytest.raises(ValueError, match="Unknown argument"):
        verify_claim(cat, {"citation": "FOIA", "bogus": 1}, signer=signer, today=TODAY)
    with pytest.raises(ValueError, match="after today"):
        verify_claim(
            cat,
            {
                "citation": "Freedom of Information Act 2011, s. 1",
                "as_of": (TODAY + timedelta(days=1)).isoformat(),
            },
            signer=signer,
            today=TODAY,
        )
    with pytest.raises(VerifyToolError) as exc:
        verify_claim(cat, {"citation": "Freedom of Information Act 2011, s. 1"}, signer=None, today=TODAY)
    assert exc.value.code == "temporarily_unavailable"


def test_blank_optional_arguments_count_as_not_sent(cat: LegacyCatalogue, signer: ReceiptSigner):
    # Gemini sends every argument a tool lists, with "" for the ones it has no value for.
    plain = verify_claim(cat, {"citation": "Freedom of Information Act 2011"}, signer=signer, today=TODAY)
    blanks = verify_claim(
        cat,
        {
            "citation": "Freedom of Information Act 2011",
            "anchor_id": "",
            "as_of": "",
            "language": " ",
            "jurisdiction": None,
        },
        signer=signer,
        today=TODAY,
    )
    assert blanks["claim"] == plain["claim"] == {"citation": "Freedom of Information Act 2011"}
    assert blanks["receipt"]["status"] == plain["receipt"]["status"]
    # Blank on both sides still names no part, and the message says so.
    with pytest.raises(InvalidClaimArguments, match="citation or anchor_id"):
        verify_claim(cat, {"citation": "", "anchor_id": ""}, signer=signer, today=TODAY)
    # A blank quote is not optional: it stays an error.
    with pytest.raises(InvalidClaimArguments, match="quote"):
        verify_claim(
            cat, {"citation": "Freedom of Information Act 2011", "quote": ""}, signer=signer, today=TODAY
        )


def test_allowed_source_ids_enforced_before_signing(cat: LegacyCatalogue, signer: ReceiptSigner):
    allowed = {"ng-foi-act-2011"}
    # Off-list public record (even with part: null like no_part_named or text_not_available) -> unavailable
    with pytest.raises(VerifyToolError) as exc1:
        verify_claim(
            cat,
            {"citation": "Metadata Only Act 2020"},
            signer=signer,
            allowed_source_ids=allowed,
            today=TODAY,
        )
    assert exc1.value.code == "unavailable"
    assert "records.json" in exc1.value.message

    with pytest.raises(VerifyToolError) as exc2:
        verify_claim(cat, {"anchor_id": "anc-rep-s1"}, signer=signer, allowed_source_ids=allowed, today=TODAY)
    assert exc2.value.code == "unavailable"

    # Never-held or whole-record-withdrawn citation -> signed not_in_catalogue (NOT unavailable)
    resp = verify_claim(
        cat,
        {"citation": "Nonexistent Nigerian Act 1990, s. 5"},
        signer=signer,
        allowed_source_ids=allowed,
        today=TODAY,
    )
    r = _assert_valid_response(resp, signer)
    assert r.status == "not_in_catalogue"

    resp_w = verify_claim(
        cat,
        {"citation": "Withdrawn Record Act 2019, s. 1"},
        signer=signer,
        allowed_source_ids=allowed,
        today=TODAY,
    )
    rw = _assert_valid_response(resp_w, signer)
    assert rw.status == "not_in_catalogue"


def test_all_13_status_rows(cat: LegacyCatalogue, signer: ReceiptSigner, monkeypatch):
    # Row 1: not_in_catalogue
    r1 = _assert_valid_response(
        verify_claim(cat, {"citation": "Imaginary Act 2099, s. 1"}, signer=signer, today=TODAY), signer
    )
    assert r1.status == "not_in_catalogue" and r1.part is None

    # Row 2a: multiple works matched (even when only one has public text) -> ambiguous
    r2a = _assert_valid_response(
        verify_claim(cat, {"citation": "Shared Act, s. 1"}, signer=signer, today=TODAY), signer
    )
    assert r2a.status == "ambiguous" and r2a.part is None
    assert len(r2a.alternatives) == 2
    assert {a.source_id for a in r2a.alternatives} == {"ng-shared-1", "ng-shared-2"}

    # Row 2b: one record, no part -> no_part_named
    r2b = _assert_valid_response(
        verify_claim(cat, {"citation": "Freedom of Information Act 2011"}, signer=signer, today=TODAY), signer
    )
    assert r2b.status == "no_part_named" and r2b.part is None

    # Row 3: text_not_available (metadata-only record AND unknown anchor_id)
    r3a = _assert_valid_response(
        verify_claim(cat, {"citation": "Metadata Only Act 2020, s. 1"}, signer=signer, today=TODAY), signer
    )
    assert r3a.status == "text_not_available" and r3a.part is None
    r3b = _assert_valid_response(
        verify_claim(cat, {"anchor_id": "anc-does-not-exist"}, signer=signer, today=TODAY), signer
    )
    assert r3b.status == "text_not_available" and r3b.part is None

    # Row 4: not_in_that_language
    r4 = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Freedom of Information Act 2011, s. 1(1)", "language": "fr"},
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r4.status == "not_in_that_language" and r4.part is None

    # Row 5: several_texts without date -> ambiguous; with date -> 1 used / 0 -> not_in_force
    r5_amb = _assert_valid_response(
        verify_claim(cat, {"citation": "Multi Version Act, s. 1"}, signer=signer, today=TODAY), signer
    )
    assert r5_amb.status == "ambiguous" and len(r5_amb.alternatives) == 2
    assert all(a.reason == "several_texts" for a in r5_amb.alternatives)

    r5_one = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Multi Version Act, s. 1",
                "as_of": "2018-06-01",
                "quote": "Original 2015 wording of the Multi Version Act section one for testing.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r5_one.status == "words_found" and r5_one.part.version_id == "ver-multi-1"

    r5_none = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Multi Version Act, s. 1", "as_of": "2010-01-01"},
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r5_none.status == "not_in_force" and r5_none.part is None

    # Row 6: part_not_found with nearest enclosing part
    r6 = _assert_valid_response(
        verify_claim(
            cat, {"citation": "Freedom of Information Act 2011, s. 1(9)"}, signer=signer, today=TODAY
        ),
        signer,
    )
    assert r6.status == "part_not_found" and r6.part is None
    assert r6.nearest is not None and r6.nearest.anchor_id == "anc-foi-s1"

    # Row 6 via replaced text missing part in newest text -> part_not_found with newer_text
    r6_repl = _assert_valid_response(
        verify_claim(cat, {"anchor_id": "anc-repl-old-s2"}, signer=signer, today=TODAY), signer
    )
    assert r6_repl.status == "part_not_found" and r6_repl.part is None
    assert r6_repl.findings.newer_text == {
        "version_id": "ver-repl-new",
        "anchor_id": None,
        "same_words": False,
    }

    # Replaced text WITH matching part in newest text -> receipt for newest part + newer_text set
    r_repl_ok = _assert_valid_response(
        verify_claim(
            cat,
            {
                "anchor_id": "anc-repl-old-s1",
                "quote": "Corrected wording of section one in the newest public text of the Act.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r_repl_ok.status == "words_found"
    assert r_repl_ok.part.anchor_id == "anc-repl-new-s1"
    assert r_repl_ok.findings.newer_text["version_id"] == "ver-repl-new"

    # Row 7: not_in_force (repealed Act)
    r7 = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Repealed Licensing Act 1999, s. 1",
                "quote": "Completely different words that are not in the repealed section at all.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r7.status == "not_in_force"
    assert r7.findings.quote == "absent"
    assert r7.findings.opening_text is not None  # both findings preserved on receipt

    # Row 8: does_not_bind (Nigerian Act checked against Rwanda)
    r8 = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Freedom of Information Act 2011, s. 1(1)", "jurisdiction": "rw"},
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r8.status == "does_not_bind"
    assert r8.findings.applicability.binds == "not_bound"

    # Row 9: wording_differs with opening_text
    r9 = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Freedom of Information Act 2011, s. 1(1)",
                "quote": "This sentence was hallucinated by a language model and is not in the Act.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r9.status == "wording_differs"
    assert r9.findings.quote == "absent"
    assert r9.findings.opening_text.startswith("(1) Notwithstanding")

    # Row 10: located (no quote given; statement starts with "No words were checked.")
    r10 = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Freedom of Information Act 2011, s. 1(1)", "jurisdiction": "ng"},
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r10.status == "located"
    assert r10.findings.quote == "not_given"
    assert r10.statement.startswith("No words were checked.")

    # Row 11: words_found_with_omissions
    r11 = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Freedom of Information Act 2011, s. 1(1)",
                "quote": "Notwithstanding anything contained ... access or request information is hereby established.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r11.status == "words_found_with_omissions"
    assert r11.findings.quote == "with_omissions"
    assert len(r11.findings.omitted) == 1

    # Row 13: words_found (record-level currentness + exact quote + reading echoed only in claim)
    resp13 = verify_claim(
        cat,
        {
            "citation": "Freedom of Information Act 2011, s. 1(1)",
            "quote": "Notwithstanding anything contained in any other Act, Law or Regulation,",
            "jurisdiction": "ng",
            "reading": "CALLER_READING_CANARY_DO_NOT_SIGN",
        },
        signer=signer,
        today=TODAY,
    )
    r13 = _assert_valid_response(resp13, signer)
    assert r13.status == "words_found"
    assert r13.findings.quote == "exact"
    assert resp13["claim"]["reading"] == "CALLER_READING_CANARY_DO_NOT_SIGN"
    assert "CALLER_READING_CANARY_DO_NOT_SIGN" not in json.dumps(resp13["receipt"])

    # Row 12: matches (provision-level in_force currentness)
    monkeypatch.setattr(
        "retrieval.legal_time.Events.currentness",
        lambda self, *args, **kwargs: Currentness(
            state="in_force",
            basis="provision",
            assessed_at=TODAY,
            algorithm="currentness-v1",
        ),
    )
    r12 = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Freedom of Information Act 2011, s. 1(1)",
                "quote": "Notwithstanding anything contained in any other Act, Law or Regulation,",
                "jurisdiction": "ng",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert r12.status == "matches"
    assert r12.findings.currentness.basis == "provision"
    assert "reviewed legal events" in r12.statement


# --- Fixes after the 2026-11-23 review ---------------------------------------------------------------


def test_a_hyphen_joined_prefix_or_suffix_is_part_of_the_word():
    """M3: quoting "public institution…" from "non-public institution…" dropped the negation and still
    gave a signed match, because the hyphen counted as a word break."""
    part = (
        "A non-public institution shall not be required to disclose its records to any applicant "
        "under this Act."
    )
    dropped = "public institution shall not be required to disclose its records"
    assert match_quote(part, dropped)["quote"] == "absent"
    assert (
        match_quote(part, "non-public institution shall not be required to disclose its records")["quote"]
        == "exact"
    )
    assert match_quote(part, "A non-public institution shall not be required to disclose")["quote"] == "exact"

    # U+2010 and U+2011 are hyphens too; the normal-form path sees them as "-".
    for hyphen in ("‐", "‑"):
        published = part.replace("non-public", f"non{hyphen}public")
        assert match_quote(published, dropped)["quote"] == "absent"
        assert (
            match_quote(published, "non-public institution shall not be required to disclose its records")[
                "quote"
            ]
            == "normalised"
        )

    # The same at the quote's end: "tax" is not a whole word in "tax-exempt".
    ending = "Every body corporate shall be subject to tax-exempt status review by the Service each year."
    assert match_quote(ending, "Every body corporate shall be subject to tax")["quote"] == "absent"
    assert match_quote(ending, "Every body corporate shall be subject to tax-exempt")["quote"] == "exact"

    # With words left out, each piece keeps the rule.
    assert (
        match_quote(part, "public institution shall not ... its records to any applicant")["quote"]
        == "absent"
    )

    # A long dash between words is punctuation, and a hyphen at a line end is not joined.
    dashed = "The Commission—public institution as it is—shall publish its records every year."
    assert match_quote(dashed, "public institution as it is—shall publish its records")["quote"] == "exact"
    assert (
        match_quote(dashed, "public institution as it is - shall publish its records every year")["quote"]
        == "absent"
    )
    line_end = "A non-\npublic institution shall not be required to disclose its records to anyone."
    assert (
        match_quote(line_end, "public institution shall not be required to disclose its records")["quote"]
        == "exact"
    )


def test_citation_and_anchor_id_together_are_refused(cat: LegacyCatalogue, signer: ReceiptSigner):
    """M4: with both, the part came from anchor_id while the unchecked citation went into the digest, so
    a receipt for FOI s. 1(1) verified beside a claim naming another law."""
    quote = "the right of any person to access or request information is hereby established"
    with pytest.raises(ValueError, match="not both"):
        verify_claim(
            cat,
            {"anchor_id": "anc-foi-s1-1", "citation": "Companies Act 2020, s. 999", "quote": quote},
            signer=signer,
            today=TODAY,
        )
    # Each alone still works, and its digest covers what was sent.
    for claim in (
        {"anchor_id": "anc-foi-s1-1", "quote": quote},
        {"citation": "Freedom of Information Act 2011, s. 1(1)", "quote": quote},
    ):
        resp = verify_claim(cat, claim, signer=signer, today=TODAY)
        receipt = _assert_valid_response(resp, signer)
        assert receipt.part is not None and receipt.part.anchor_id == "anc-foi-s1-1"
        assert receipt.claim.digest == claim_digest(claim)


def test_a_citation_naming_records_on_and_off_the_list_is_answered_for_those_on_it(
    cat: LegacyCatalogue, signer: ReceiptSigner
):
    """M9: "Shared Act" names ng-shared-1 and ng-shared-2. With only the first on Law check's list the
    check was refused as `unavailable`; resolve_citation answered the same citation as resolved."""
    resp = verify_claim(
        cat, {"citation": "Shared Act, s. 1"}, signer=signer, allowed_source_ids={"ng-shared-1"}, today=TODAY
    )
    receipt = _assert_valid_response(resp, signer)
    assert receipt.status != "ambiguous"
    assert receipt.part is not None and receipt.part.source_id == "ng-shared-1"
    assert "ng-shared-2" not in json.dumps(resp)

    # None of the records it names is on the list: still `unavailable`, and nothing is signed.
    with pytest.raises(VerifyToolError) as refused:
        verify_claim(
            cat,
            {"citation": "Shared Act, s. 1"},
            signer=signer,
            allowed_source_ids={"ng-foi-act-2011"},
            today=TODAY,
        )
    assert refused.value.code == "unavailable"

    # Both on the list: ambiguous, as without a list.
    both = verify_claim(
        cat,
        {"citation": "Shared Act, s. 1"},
        signer=signer,
        allowed_source_ids={"ng-shared-1", "ng-shared-2"},
        today=TODAY,
    )
    assert _assert_valid_response(both, signer).status == "ambiguous"


def test_the_quotes_words_decide_which_of_several_texts_is_checked(
    cat: LegacyCatalogue, signer: ReceiptSigner
):
    """A citation naming a part in more than one text of a record (Rwanda's three language editions, two
    readings of one Act) is checked in the text whose words hold the quote; never chosen otherwise."""
    amended = "Amended 2022 wording of the Multi Version Act section one for testing."
    receipt = _assert_valid_response(
        verify_claim(
            cat, {"citation": "Multi Version Act, s. 1", "quote": amended}, signer=signer, today=TODAY
        ),
        signer,
    )
    assert receipt.status == "words_found" and receipt.part.version_id == "ver-multi-2"
    original = "Original 2015 wording of the Multi Version Act section one for testing."
    receipt = _assert_valid_response(
        verify_claim(
            cat, {"citation": "Multi Version Act, s. 1", "quote": original}, signer=signer, today=TODAY
        ),
        signer,
    )
    assert receipt.part.version_id == "ver-multi-1" and receipt.findings.quote == "exact"
    # Words in neither text, or no quote at all: both texts are still listed for the caller to choose.
    for quote in ("Wording found in neither text of the Multi Version Act at all.", None):
        claim = {"citation": "Multi Version Act, s. 1", **({"quote": quote} if quote else {})}
        receipt = _assert_valid_response(verify_claim(cat, claim, signer=signer, today=TODAY), signer)
        assert receipt.status == "ambiguous" and len(receipt.alternatives) == 2
    # A quote too short is refused as it is for one part.
    with pytest.raises(VerifyToolError) as error:
        verify_claim(
            cat, {"citation": "Multi Version Act, s. 1", "quote": "Amended 2022"}, signer=signer, today=TODAY
        )
    assert error.value.code == "quote_too_short"
    # Two records sharing a name stay a choice for the caller, words or not.
    receipt = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Shared Act, s. 1",
                "quote": "Shared Act provision text of sufficient length for checking.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert receipt.status == "ambiguous"


def test_a_quote_in_no_text_is_answered_in_the_text_clearly_closest_to_it(
    cat: LegacyCatalogue, signer: ReceiptSigner
):
    """Found live on 2026-11-28: a one-word slip in an English quote of a Rwandan article came back as a
    choice among its three language texts. The text clearly closest to the quote is now read, so the
    answer is "not found" there with the section's opening words, from which the page shows the slip."""
    true = "Every person is equal before the law and enjoys equal protection of the law."
    receipt = _assert_valid_response(
        verify_claim(cat, {"citation": "Two Languages Act, s. 1", "quote": true}, signer=signer, today=TODAY),
        signer,
    )
    assert receipt.findings.quote == "exact" and receipt.part.version_id == "ver-lang-en"
    slipped = true.replace("equal protection", "special protection")
    receipt = _assert_valid_response(
        verify_claim(
            cat, {"citation": "Two Languages Act, s. 1", "quote": slipped}, signer=signer, today=TODAY
        ),
        signer,
    )
    assert receipt.status == "wording_differs" and receipt.part.version_id == "ver-lang-en"
    assert receipt.findings.quote == "absent" and "equal protection" in receipt.findings.opening_text
    # The same slip in French is answered in the French text.
    receipt = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Two Languages Act, s. 1",
                "quote": "Toute personne est égale devant la loi et jouit d'une protection spéciale.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert receipt.status == "wording_differs" and receipt.part.version_id == "ver-lang-fr"
    # Two readings of one Act a quote is close to alike, or no quote at all: still a choice for the caller.
    for claim in (
        {
            "citation": "Multi Version Act, s. 1",
            "quote": "Amended 2022 wording of the Multi Version Act section two for testing.",
        },
        {"citation": "Two Languages Act, s. 1"},
    ):
        receipt = _assert_valid_response(verify_claim(cat, claim, signer=signer, today=TODAY), signer)
        assert receipt.status == "ambiguous" and len(receipt.alternatives) == 2
    receipt = _assert_valid_response(
        verify_claim(
            cat,
            {
                "citation": "Multi Version Act, s. 1",
                "quote": "Wording found in neither text of the Multi Version Act at all.",
            },
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert receipt.status == "ambiguous" and len(receipt.alternatives) == 2


def test_the_same_words_in_two_texts_are_checked_in_the_one_in_force(
    cat: LegacyCatalogue, signer: ReceiptSigner
):
    quote = "This Act may be cited as the Two Readings Act and applies throughout the federation."
    receipt = _assert_valid_response(
        verify_claim(cat, {"citation": "Two Readings Act, s. 1", "quote": quote}, signer=signer, today=TODAY),
        signer,
    )
    assert receipt.part.version_id == "ver-two-2" and receipt.findings.quote == "exact"
    assert receipt.status in ("words_found", "matches")
    # On a date the first reading was in force, that one is read.
    receipt = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Two Readings Act, s. 1", "quote": quote, "as_of": "2017-03-01"},
            signer=signer,
            today=TODAY,
        ),
        signer,
    )
    assert receipt.part.version_id == "ver-two-1"


def test_a_published_record_with_a_part_law_check_cannot_read_is_a_part_not_found(
    cat: LegacyCatalogue, signer: ReceiptSigner
):
    """The record is in the catalogue, so "not in catalogue" would be wrong."""
    receipt = _assert_valid_response(
        verify_claim(
            cat, {"citation": "Freedom of Information Act 2011, Chapter Q"}, signer=signer, today=TODAY
        ),
        signer,
    )
    assert receipt.status == "part_not_found" and receipt.part is None
    for citation in ("Imaginary Act 2099, Chapter Q", "Withdrawn Record Act 2019, Chapter Q"):
        receipt = _assert_valid_response(
            verify_claim(cat, {"citation": citation}, signer=signer, today=TODAY), signer
        )
        assert receipt.status == "not_in_catalogue"
    # Off a caller's list, the record is not named for it.
    receipt = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Repealed Licensing Act 1999, Chapter Q"},
            signer=signer,
            allowed_source_ids={"ng-foi-act-2011"},
            today=TODAY,
        ),
        signer,
    )
    assert receipt.status == "not_in_catalogue"


def test_a_long_citation_is_accepted_up_to_600_characters(cat: LegacyCatalogue, signer: ReceiptSigner):
    long_title = "Imaginary Act " + "approving the ratification of a long agreement " * 10
    receipt = _assert_valid_response(
        verify_claim(cat, {"citation": long_title[:590] + ", s. 1"}, signer=signer, today=TODAY), signer
    )
    assert receipt.status == "not_in_catalogue"
    with pytest.raises(ValueError, match="1 to 600"):
        verify_claim(cat, {"citation": "x" * 601}, signer=signer, today=TODAY)


def test_a_record_named_with_an_unread_part_is_admitted_by_the_plan_like_any_other(
    cat: LegacyCatalogue, signer: ReceiptSigner
):
    asked = []
    receipt = _assert_valid_response(
        verify_claim(
            cat,
            {"citation": "Freedom of Information Act 2011, Chapter Q"},
            signer=signer,
            check_jurisdiction=asked.append,
            today=TODAY,
        ),
        signer,
    )
    assert receipt.status == "part_not_found" and asked == ["ng"]
