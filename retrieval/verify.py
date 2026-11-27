"""Deterministic Law check verifier (oa-verify 1.0): citation/anchor lookup, quote matching,
currentness & applicability checks, status decision table, and signed receipt assembly."""

import os
import re
import time
import unicodedata
from datetime import UTC, date, datetime

from api.catalogue import LegacyCatalogue, SourceUnavailable, _in_force, _speaks
from api.receipts import ReceiptSigner, claim_digest
from retrieval.check import Checker
from retrieval.pack import dataset_revision
from schema.public import CheckItem, NodeViewRecord, Receipt

ALLOWED_ARGUMENTS = frozenset(
    {"citation", "anchor_id", "quote", "as_of", "jurisdiction", "language", "reading"}
)
JURISDICTION_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]{1,12})?$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
MAX_PART_BYTES = 256 * 1024
MIN_QUOTE_LEN = 40
MIN_OMISSION_PIECE_LEN = 12
MAX_OMISSION_SPREAD = 4000
CONTEXT_LEN = 200
OPENING_LEN = 400

STRIP_CHARS = frozenset({"\u00ad", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"})
LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}
CURLY_SINGLE = frozenset({"\u2018", "\u2019", "\u201a", "\u201b"})
CURLY_DOUBLE = frozenset({"\u201c", "\u201d", "\u201e", "\u201f"})
DASHES = frozenset({"\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212"})
# A true hyphen between two word characters joins them into one word ("non-public", "tax-exempt").
# Longer dashes are punctuation between words, so they stay word breaks.
HYPHEN_JOINERS = frozenset({"-", "\u2010", "\u2011"})

_ELLIPSIS_LEAD = re.compile(r"^(?:\s|…|\.\.\.)+")
_ELLIPSIS_TRAIL = re.compile(r"(?:\s|…|\.\.\.)+$")
_ELLIPSIS_SPLIT = re.compile(r"(?:…|\.\.\.)+")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class VerifyToolError(Exception):
    """Structured tool-level error returned as an MCP tool error (never an uncaught exception)."""

    def __init__(self, code: str, message: str, *, extra: dict | None = None):
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(f"{code}: {message}")


def _ulid() -> str:
    """Generate a 26-character Crockford Base32 ULID."""
    ts_ms = int(time.time() * 1000)
    rand_int = int.from_bytes(os.urandom(10), "big")
    value = (ts_ms << 80) | rand_int
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _iter_nfc_clusters(text: str):
    """Yield ``(nfc_cluster, start_idx, end_idx)`` grouping each base char with combining marks."""
    n = len(text)
    i = 0
    while i < n:
        j = i + 1
        while j < n and unicodedata.combining(text[j]) != 0:
            j += 1
        cluster = text[i:j]
        yield unicodedata.normalize("NFC", cluster), i, j
        i = j


def _nfc_with_map(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return NFC-normalized string and per-character ``(orig_start, orig_end)`` map."""
    out: list[str] = []
    index_map: list[tuple[int, int]] = []
    for cluster, start, end in _iter_nfc_clusters(text):
        for ch in cluster:
            out.append(ch)
            index_map.append((start, end))
    return "".join(out), index_map


def _normalize_with_map(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Apply §8 Step 3 normal form while tracking original code-point slices for every emitted char."""
    out: list[str] = []
    index_map: list[tuple[int, int]] = []
    ws_start: int | None = None
    ws_end: int | None = None

    for cluster, c_start, c_end in _iter_nfc_clusters(text):
        for ch in cluster:
            if ch in STRIP_CHARS:
                continue
            if ch.isspace():
                if out:
                    if ws_start is None:
                        ws_start = c_start
                    ws_end = c_end
                continue
            if ws_start is not None:
                out.append(" ")
                index_map.append((ws_start, ws_end))
                ws_start = None
                ws_end = None
            if ch in LIGATURES:
                for letter in LIGATURES[ch]:
                    out.append(letter)
                    index_map.append((c_start, c_end))
            elif ch in CURLY_SINGLE:
                out.append("'")
                index_map.append((c_start, c_end))
            elif ch in CURLY_DOUBLE:
                out.append('"')
                index_map.append((c_start, c_end))
            elif ch in DASHES:
                out.append("-")
                index_map.append((c_start, c_end))
            else:
                out.append(ch)
                index_map.append((c_start, c_end))

    return "".join(out), index_map


def normalize_quote(text: str) -> str:
    """Apply §8 Step 3 normal form: NFC, strip soft hyphens/zero-width chars, expand ligatures,
    straighten curly quotes, normalize dashes to hyphen, collapse whitespace, keep case."""
    norm, _ = _normalize_with_map(text)
    return norm


def _find_bounded(haystack: str, needle: str, start: int = 0, published=None) -> int:
    """Find first occurrence of ``needle`` in ``haystack`` at or after ``start`` with punctuation-safe
    word boundaries.

    A quote that starts or ends on a word character must start or end a whole word. A hyphen straight
    between two word characters joins them, so "public institution" is not found in "non-public
    institution", nor "subject to tax" in "subject to tax-exempt". ``published(i)`` gives the published
    character behind ``haystack[i]``: the normal form turns every dash into "-", and only a true hyphen
    joins words. Hyphenated line ends (a hyphen followed by white space) are not joined."""
    if not needle:
        return -1
    starts_word = _is_word_char(needle[0])
    ends_word = _is_word_char(needle[-1])

    def joiner(i: int) -> bool:
        return (published(i) if published is not None else haystack[i]) in HYPHEN_JOINERS

    pos = start
    while True:
        idx = haystack.find(needle, pos)
        if idx == -1:
            return -1
        end = idx + len(needle)
        left_ok = (
            not starts_word
            or idx == 0
            or not (
                _is_word_char(haystack[idx - 1])
                or (idx >= 2 and joiner(idx - 1) and _is_word_char(haystack[idx - 2]))
            )
        )
        right_ok = (
            not ends_word
            or end == len(haystack)
            or not (
                _is_word_char(haystack[end])
                or (end + 1 < len(haystack) and joiner(end) and _is_word_char(haystack[end + 1]))
            )
        )
        if left_ok and right_ok:
            return idx
        pos = idx + 1


def _strip_outer_ellipses(quote: str) -> str:
    stripped = _ELLIPSIS_LEAD.sub("", quote)
    stripped = _ELLIPSIS_TRAIL.sub("", stripped)
    return stripped


def _opening_text(part_text: str, limit: int = OPENING_LEN) -> str:
    """Return up to ``limit`` code points of ``part_text``, trimming to a word boundary where possible."""
    if len(part_text) <= limit:
        return part_text
    chunk = part_text[:limit]
    if _is_word_char(chunk[-1]) and _is_word_char(part_text[limit]):
        cut = limit
        while cut > 0 and _is_word_char(chunk[cut - 1]):
            cut -= 1
        if cut >= limit // 2:
            chunk = chunk[:cut]
    return chunk.rstrip()


def _absent_result(part_text: str) -> dict:
    return {
        "quote": "absent",
        "matched_text": None,
        "opening_text": _opening_text(part_text, OPENING_LEN),
        "context_before": None,
        "context_after": None,
        "omitted": [],
    }


def match_quote(part_text: str, quote: str) -> dict:
    """Match ``quote`` against ``part_text`` per §8 Step 3.

    Raises ``ValueError`` if the quote is empty after stripping ellipses.
    Raises ``VerifyToolError("quote_too_short", ...)`` or ``VerifyToolError("quote_too_spread", ...)``.
    """
    stripped = _strip_outer_ellipses(quote)
    if not normalize_quote(stripped):
        raise ValueError("Quote is empty after stripping ellipses")

    raw_pieces = _ELLIPSIS_SPLIT.split(stripped)
    norm_pieces = [p for p in (normalize_quote(x) for x in raw_pieces) if p]
    if not norm_pieces:
        raise ValueError("Quote is empty after stripping ellipses")

    norm_part, norm_map = _normalize_with_map(part_text)

    def published(i: int) -> str:
        """The published character behind position ``i`` of the normal form."""
        return part_text[norm_map[i][0] : norm_map[i][1]]

    if len(norm_pieces) > 1:
        for piece in norm_pieces:
            if len(piece) < MIN_OMISSION_PIECE_LEN:
                raise VerifyToolError(
                    "quote_too_short",
                    f"Each quoted segment must be at least {MIN_OMISSION_PIECE_LEN} characters after normalisation.",
                )
        if sum(len(p) for p in norm_pieces) < MIN_QUOTE_LEN:
            raise VerifyToolError(
                "quote_too_short",
                f"Quote must be at least {MIN_QUOTE_LEN} characters after normalisation.",
            )
        cursor = 0
        matched_spans: list[tuple[int, int]] = []
        for piece in norm_pieces:
            idx = _find_bounded(norm_part, piece, cursor, published)
            if idx == -1:
                return _absent_result(part_text)
            end_idx = idx + len(piece)
            matched_spans.append((norm_map[idx][0], norm_map[end_idx - 1][1]))
            cursor = end_idx
        orig_start = matched_spans[0][0]
        orig_end = matched_spans[-1][1]
        if orig_end - orig_start > MAX_OMISSION_SPREAD:
            raise VerifyToolError(
                "quote_too_spread",
                f"Quoted segments span more than {MAX_OMISSION_SPREAD:,} characters in the published text.",
            )
        omitted: list[list[int]] = []
        for (_, prev_end), (next_start, _) in zip(matched_spans, matched_spans[1:], strict=False):
            if next_start > prev_end:
                omitted.append([prev_end - orig_start, next_start - orig_start])
        return {
            "quote": "with_omissions",
            "matched_text": part_text[orig_start:orig_end],
            "opening_text": None,
            "context_before": part_text[max(0, orig_start - CONTEXT_LEN) : orig_start],
            "context_after": part_text[orig_end : min(len(part_text), orig_end + CONTEXT_LEN)],
            "omitted": omitted,
        }

    # Single contiguous quote
    norm_q = norm_pieces[0]
    if len(norm_q) < MIN_QUOTE_LEN and norm_q != norm_part:
        raise VerifyToolError(
            "quote_too_short",
            f"Quote must be at least {MIN_QUOTE_LEN} characters after normalisation, unless it is the whole of the part's words.",
        )

    # 1. Try exact (NFC only)
    nfc_q = unicodedata.normalize("NFC", stripped)
    nfc_part, nfc_map = _nfc_with_map(part_text)
    exact_idx = _find_bounded(nfc_part, nfc_q, 0)
    if exact_idx != -1:
        orig_start = nfc_map[exact_idx][0]
        orig_end = nfc_map[exact_idx + len(nfc_q) - 1][1]
        return {
            "quote": "exact",
            "matched_text": part_text[orig_start:orig_end],
            "opening_text": None,
            "context_before": part_text[max(0, orig_start - CONTEXT_LEN) : orig_start],
            "context_after": part_text[orig_end : min(len(part_text), orig_end + CONTEXT_LEN)],
            "omitted": [],
        }

    # 2. Try normalised
    norm_idx = _find_bounded(norm_part, norm_q, 0, published)
    if norm_idx != -1:
        orig_start = norm_map[norm_idx][0]
        orig_end = norm_map[norm_idx + len(norm_q) - 1][1]
        return {
            "quote": "normalised",
            "matched_text": part_text[orig_start:orig_end],
            "opening_text": None,
            "context_before": part_text[max(0, orig_start - CONTEXT_LEN) : orig_start],
            "context_after": part_text[orig_end : min(len(part_text), orig_end + CONTEXT_LEN)],
            "omitted": [],
        }

    return _absent_result(part_text)


def _validate_arguments(arguments: dict, today: date) -> tuple[dict, dict]:
    """Validate check_claim arguments per §7. Returns ``(parsed, echoed_claim)``."""
    if not isinstance(arguments, dict):
        raise ValueError("Arguments must be a JSON object")
    unknown = set(arguments) - ALLOWED_ARGUMENTS
    if unknown:
        raise ValueError(f"Unknown argument(s): {', '.join(sorted(unknown))}")

    citation = arguments.get("citation")
    anchor_id = arguments.get("anchor_id")
    if citation is None and anchor_id is None:
        raise ValueError("One of citation or anchor_id is required")
    if "citation" in arguments and "anchor_id" in arguments:
        # With both, the part is found by anchor_id and the citation would be digested unchecked: a
        # receipt for one section could then be shown beside a claim naming another.
        raise ValueError("Send citation or anchor_id, not both")

    echoed: dict[str, object] = {}

    if "citation" in arguments:
        if not isinstance(citation, str) or not (1 <= len(citation) <= 600) or not citation.strip():
            raise ValueError("citation must be a non-empty string of 1 to 600 characters")
        echoed["citation"] = citation

    if "anchor_id" in arguments:
        if not isinstance(anchor_id, str) or not (1 <= len(anchor_id) <= 160) or not anchor_id.strip():
            raise ValueError("anchor_id must be a non-empty string of 1 to 160 characters")
        echoed["anchor_id"] = anchor_id

    quote = arguments.get("quote")
    if "quote" in arguments:
        if not isinstance(quote, str) or not (1 <= len(quote) <= 2000):
            raise ValueError("quote must be a string of 1 to 2,000 characters")
        echoed["quote"] = quote

    as_of: date | None = None
    if "as_of" in arguments:
        raw_as_of = arguments["as_of"]
        if isinstance(raw_as_of, date) and not isinstance(raw_as_of, datetime):
            as_of = raw_as_of
            echoed["as_of"] = as_of.isoformat()
        elif isinstance(raw_as_of, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_as_of):
            try:
                as_of = date.fromisoformat(raw_as_of)
            except ValueError as exc:
                raise ValueError("as_of must be a valid ISO date (YYYY-MM-DD)") from exc
            echoed["as_of"] = raw_as_of
        else:
            raise ValueError("as_of must be an ISO date string (YYYY-MM-DD)")
        if as_of > today:
            raise ValueError("as_of must not be after today in UTC")

    jurisdiction = arguments.get("jurisdiction")
    if "jurisdiction" in arguments:
        if (
            not isinstance(jurisdiction, str)
            or not (2 <= len(jurisdiction) <= 40)
            or not JURISDICTION_RE.fullmatch(jurisdiction)
        ):
            raise ValueError("jurisdiction must be a valid jurisdiction code (e.g. 'ng')")
        echoed["jurisdiction"] = jurisdiction

    language = arguments.get("language")
    if "language" in arguments:
        if (
            not isinstance(language, str)
            or not (2 <= len(language) <= 20)
            or not LANGUAGE_RE.fullmatch(language)
        ):
            raise ValueError("language must be a valid language tag (e.g. 'en')")
        echoed["language"] = language

    reading = arguments.get("reading")
    if "reading" in arguments:
        if not isinstance(reading, str) or len(reading) > 600:
            raise ValueError("reading must be a string of up to 600 characters")
        echoed["reading"] = reading

    parsed = {
        "citation": citation,
        "anchor_id": anchor_id,
        "quote": quote,
        "as_of": as_of,
        "jurisdiction": jurisdiction,
        "language": language,
        "reading": reading,
    }
    return parsed, echoed


def _currentness_clause(currentness: dict, date_sent: bool) -> str:
    state = currentness["state"]
    basis = currentness["basis"]
    assessed_at = currentness["assessed_at"]
    if state == "in_force":
        if basis == "provision":
            return f"Open Alvary's record lists this provision as in force on {assessed_at}, from reviewed legal events."
        if date_sent:
            return (
                f"Open Alvary's record lists the text as in force on {assessed_at}, "
                "by the text's effective dates only; it holds no reviewed legal events for it."
            )
        return (
            f"Open Alvary's record lists the law as in force on {assessed_at}; "
            "it holds no reviewed legal events for it."
        )
    if state == "partly_in_force":
        return f"Open Alvary's record lists this provision as partly in force on {assessed_at}."
    if state == "contested":
        return f"Open Alvary's record lists the status on {assessed_at} as contested."
    return f"Open Alvary's record lists the status on {assessed_at} as unknown."


def _build_statement(
    status: str,
    *,
    part: dict | None,
    nearest: dict | None,
    currentness: dict | None,
    assessed_at: str,
    date_sent: bool,
    resolved_title: str | None = None,
) -> str:
    if status == "not_in_catalogue":
        return (
            "Open Alvary's catalogue does not hold a public record matching this citation. "
            "This describes Open Alvary's catalogue; it is not legal advice."
        )
    if status == "no_part_named":
        label = resolved_title or "this record"
        return (
            f"This citation names {label}, not a specific section or part. "
            "Name a section or part to check its words. "
            "This describes Open Alvary's catalogue; it is not legal advice."
        )
    if status == "text_not_available":
        return (
            "No published text is available in Open Alvary's catalogue to check this citation. "
            "This describes Open Alvary's catalogue; it is not legal advice."
        )
    if status == "not_in_that_language":
        return (
            "Open Alvary does not publish a text of this record in the requested language. "
            "This describes Open Alvary's catalogue; it is not legal advice."
        )
    if status == "ambiguous":
        return (
            "More than one record or part in Open Alvary's catalogue matches this citation. "
            "Choose one of the listed alternatives. "
            "This describes Open Alvary's catalogue; it is not legal advice."
        )
    if status == "part_not_found":
        if nearest is not None:
            return (
                f"The cited part was not found in the published text of {nearest['title']}; "
                f"the nearest enclosing part is {nearest['pinpoint']}. "
                "This describes a published text; it is not legal advice."
            )
        return (
            "The cited part was not found in the published text. "
            "This describes a published text; it is not legal advice."
        )
    if status == "not_in_force":
        if part is not None:
            loc = f"{part['pinpoint']} of {part['title']}" if part["pinpoint"] else part["title"]
            return (
                f"Open Alvary's record lists {loc} as not in force on {assessed_at}, "
                "in the text Open Alvary publishes from the official source linked here. "
                "This describes a published text; it is not legal advice."
            )
        return (
            f"No published text of this record is listed as in force on {assessed_at}. "
            "This describes Open Alvary's catalogue; it is not legal advice."
        )
    if status == "does_not_bind":
        assert part is not None
        return (
            f"Open Alvary's record lists {part['title']} as not binding in the requested jurisdiction "
            f"on {assessed_at}, in the text Open Alvary publishes from the official source linked here. "
            "This describes a published text; it is not legal advice."
        )

    assert part is not None
    loc = f"{part['pinpoint']} of {part['title']}" if part["pinpoint"] else part["title"]
    if status == "wording_differs":
        return (
            f"The quoted words are not in {loc}, in the text Open Alvary publishes "
            "from the official source linked here. "
            "This describes a published text; it is not legal advice."
        )
    curr_clause = _currentness_clause(currentness, date_sent) if currentness else ""
    if status == "located":
        return (
            f"No words were checked. {loc} was located in the text Open Alvary publishes "
            f"from the official source linked here. {curr_clause} "
            "This describes a published text; it is not legal advice."
        )
    if status == "words_found_with_omissions":
        return (
            f"The quoted words are in {loc}, with words left out, in the text Open Alvary publishes "
            f"from the official source linked here. {curr_clause} "
            "This describes a published text; it is not legal advice."
        )
    if status == "matches":
        return (
            f"The quoted words are in {loc}, in the text Open Alvary publishes "
            f"from the official source linked here. {curr_clause} "
            "This describes a published text; it is not legal advice."
        )
    # words_found
    return (
        f"The quoted words are in {loc}, in the text Open Alvary publishes "
        f"from the official source linked here. {curr_clause} "
        "This describes a published text; it is not legal advice."
    )


def _finalize_receipt(
    *,
    catalogue: LegacyCatalogue,
    signer: ReceiptSigner,
    echoed_claim: dict,
    assessed_at: date,
    date_sent: bool,
    jurisdiction: str | None,
    status: str,
    part: dict | None,
    nearest: dict | None,
    alternatives: list[dict],
    findings: dict,
    resolved_title: str | None = None,
) -> dict:
    issued_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    statement = _build_statement(
        status,
        part=part,
        nearest=nearest,
        currentness=findings.get("currentness"),
        assessed_at=assessed_at.isoformat(),
        date_sent=date_sent,
        resolved_title=resolved_title,
    )
    receipt_body = {
        "schema_version": "oa-receipt-1",
        "receipt_id": f"rcp_{_ulid()}",
        "issued_at": issued_at,
        "key": {"alg": "ES256", "kid": signer.kid},
        "mechanism": {"name": "oa-verify", "version": "1.0"},
        "dataset": dataset_revision(catalogue.repository.connection),
        "claim": {
            "digest": claim_digest(echoed_claim),
            "assessed_at": assessed_at.isoformat(),
            "date_sent": date_sent,
            "jurisdiction": jurisdiction,
        },
        "part": part,
        "nearest": nearest,
        "alternatives": alternatives,
        "findings": findings,
        "status": status,
        "statement": statement,
    }
    signature = signer.sign(receipt_body)
    receipt = {**receipt_body, "signature": signature}
    # Validate against canonical Pydantic wire schema to guarantee contract conformance.
    Receipt.model_validate(receipt)
    return {"receipt": receipt, "claim": echoed_claim}


def _empty_findings(
    *,
    currentness: dict | None = None,
    applicability: dict | None = None,
    newer_text: dict | None = None,
) -> dict:
    return {
        "quote": None,
        "matched_text": None,
        "opening_text": None,
        "context_before": None,
        "context_after": None,
        "omitted": [],
        "currentness": currentness,
        "applicability": applicability,
        "newer_text": newer_text,
    }


def _enforce_allowed_source(source_id: str, allowed_source_ids: set[str] | None) -> None:
    if allowed_source_ids is not None and source_id not in allowed_source_ids:
        raise VerifyToolError(
            "unavailable",
            "Law check does not cover this record. See https://open.alvary.ai/agents/records.json",
        )


HOLDS_QUOTE = frozenset({"exact", "normalised", "with_omissions"})
MAX_WORK_SPLITS = 4


def _choose_by_words(catalogue: LegacyCatalogue, candidates, quote: str, assessed_at: date):
    """The one candidate part whose published words hold ``quote``, where a citation names more than one
    part of a record (its texts in three languages, two readings of one Act): the words decide which part
    the claim is about. Where several hold it with the very same words (the same span hash) in one
    record, they are one passage in more than one of its texts, and the one in force on ``assessed_at``
    (else the latest) is read. None when no candidate holds the quote, or different parts do. A quote
    too short or too spread for every candidate raises that error, as it would for one part."""
    holding, errors = [], []
    for candidate in candidates:
        row = catalogue.repository.node(candidate.anchor_id)
        if row is None:
            continue
        node = NodeViewRecord.model_validate(row)
        try:
            version, _, _ = catalogue.version(node.version_id)
        except SourceUnavailable:
            continue
        span_bytes, _ = catalogue._span(node, version)
        if len(span_bytes) > MAX_PART_BYTES:
            continue
        try:
            found = match_quote(span_bytes.decode("utf-8"), quote)
        except VerifyToolError as error:
            errors.append(error)
            continue
        if found["quote"] in HOLDS_QUOTE:
            holding.append(candidate)
    if not holding:
        if errors and len(errors) == len(candidates):
            raise errors[0]
        return None
    if len(holding) == 1:
        return holding[0]
    if len({c.source_id for c in holding}) == 1 and len({c.span_hash for c in holding}) == 1:
        return max(
            holding,
            key=lambda c: (_in_force(c, assessed_at), c.effective_from or date.min, c.version_id),
        )
    return None


def _names_a_record(catalogue: LegacyCatalogue, citation: str, allowed_source_ids) -> bool:
    """Whether the words before one of the citation's last commas name a public record ("Law n° 1/2016
    …, Chapter One A"): the record is in the catalogue though its part was not read, so the part is not
    found rather than the record missing."""
    at = len(citation)
    for _ in range(MAX_WORK_SPLITS):
        at = citation.rfind(",", 0, at)
        if at <= 0:
            return False
        work = citation[:at].strip(" ,;")
        if not work:
            return False
        matches = catalogue.citations(work).matches
        if allowed_source_ids is not None:
            matches = [m for m in matches if m.id in allowed_source_ids]
        if matches:
            return True
    return False


def verify_claim(
    catalogue: LegacyCatalogue,
    arguments: dict,
    *,
    signer: ReceiptSigner | None = None,
    allowed_source_ids: set[str] | None = None,
    today: date | None = None,
) -> dict:
    """Deterministically verify a legal claim against the public catalogue and return a signed receipt."""
    today_utc = today or datetime.now(UTC).date()
    parsed, echoed_claim = _validate_arguments(arguments, today_utc)

    if signer is None:
        raise VerifyToolError("temporarily_unavailable", "Receipt signing key is not configured.")

    citation: str | None = parsed["citation"]
    anchor_id: str | None = parsed["anchor_id"]
    quote: str | None = parsed["quote"]
    as_of: date | None = parsed["as_of"]
    jurisdiction: str | None = parsed["jurisdiction"]
    language: str | None = parsed["language"]

    assessed_at = as_of or today_utc
    date_sent = as_of is not None
    checker_as_of = as_of if (as_of is not None and as_of != today_utc) else None

    def finish(
        status: str,
        *,
        part: dict | None = None,
        nearest: dict | None = None,
        alternatives: list[dict] | None = None,
        findings: dict | None = None,
        resolved_title: str | None = None,
    ) -> dict:
        return _finalize_receipt(
            catalogue=catalogue,
            signer=signer,
            echoed_claim=echoed_claim,
            assessed_at=assessed_at,
            date_sent=date_sent,
            jurisdiction=jurisdiction,
            status=status,
            part=part,
            nearest=nearest,
            alternatives=alternatives or [],
            findings=findings or _empty_findings(),
            resolved_title=resolved_title,
        )

    target_anchor_id: str | None = None
    precomputed_check = None
    precomputed_newer_text: dict | None = None

    # --- Step 1: Locate ----------------------------------------------------------------------------
    if anchor_id is not None:
        # Enforce allowed_source_ids BEFORE signing if anchor belongs to a public record.
        node_row = catalogue.repository.node(anchor_id)
        if node_row is not None:
            owner_source_id = node_row.get("source_id") or catalogue.repository.version_owner(
                node_row["version_id"]
            )
            if owner_source_id and catalogue._public({owner_source_id}):
                _enforce_allowed_source(owner_source_id, allowed_source_ids)

        checker = Checker(catalogue, today=today_utc, withdrawn=None)
        check_page = checker.check(
            (CheckItem(anchor_id=anchor_id, as_of=checker_as_of, jurisdiction=jurisdiction),)
        )
        chk = check_page.results[0]
        if chk.result == "unavailable":
            # Row 3: not public and no successor, or never existed
            return finish("text_not_available")

        if chk.result == "newer_version":
            # Distinguish public version with newer text vs replaced non-public version
            try:
                catalogue.version(chk.version_id)
                original_public = True
            except SourceUnavailable:
                original_public = False

            precomputed_newer_text = {
                "version_id": chk.newer_version_id,
                "anchor_id": chk.newer_anchor_id,
                "same_words": chk.same_words,
            }
            if not original_public:
                if chk.newer_anchor_id is None:
                    # Row 6: replaced text missing this part in newest text
                    return finish(
                        "part_not_found",
                        findings=_empty_findings(newer_text=precomputed_newer_text),
                    )
                target_anchor_id = chk.newer_anchor_id
            else:
                target_anchor_id = chk.anchor_id
            precomputed_check = chk
        else:
            target_anchor_id = chk.anchor_id
            precomputed_check = chk
    else:
        assert citation is not None
        res = catalogue.citations(citation, as_of=None, language=language)

        # Enforce allowed_source_ids BEFORE signing. A citation that names records on and off the list
        # is answered for those on it, as resolve_citation answers it; `unavailable` only when every
        # record it names is off the list.
        if res.matches and allowed_source_ids is not None:
            off_list = [match.id for match in res.matches if match.id not in allowed_source_ids]
            if len(off_list) == len(res.matches):
                _enforce_allowed_source(off_list[0], allowed_source_ids)
            if off_list:
                from api.mcp import _filter_citation_view

                res = _filter_citation_view(res, frozenset(allowed_source_ids))

        # Row 1: no public record named. A record named with a part Law check cannot read is a part not
        # found: the record is in the catalogue.
        if not res.matches:
            if _names_a_record(catalogue, citation, allowed_source_ids):
                return finish("part_not_found")
            return finish("not_in_catalogue")

        # Row 2a: more than one work matched
        if len(res.matches) > 1:
            cand_by_source: dict[str, list] = {}
            if res.provision and res.provision.candidates:
                for c in res.provision.candidates:
                    cand_by_source.setdefault(c.source_id, []).append(c)
            alts: list[dict] = []
            for m in res.matches:
                cands = cand_by_source.get(m.id)
                if cands:
                    for c in cands:
                        alts.append(
                            {
                                "source_id": m.id,
                                "title": m.title,
                                "anchor_id": c.anchor_id,
                                "pinpoint": c.pinpoint,
                                "reason": "several_works",
                            }
                        )
                else:
                    alts.append(
                        {
                            "source_id": m.id,
                            "title": m.title,
                            "anchor_id": None,
                            "pinpoint": None,
                            "reason": "several_works",
                        }
                    )
            return finish("ambiguous", alternatives=alts)

        # Row 2b: one record named, no part
        source_view = res.matches[0]
        if res.provision is None:
            return finish("no_part_named", resolved_title=source_view.title)

        prov = res.provision
        # Row 3: no public text
        if prov.status == "unresolved" and prov.reason == "no_public_text":
            return finish("text_not_available")

        # Row 4: no text in requested language
        if prov.status == "unresolved" and prov.reason == "no_text_in_scope":
            return finish("not_in_that_language")

        # Row 5: ambiguous provision. With a quote, the part whose words hold it is the one checked.
        if prov.status == "ambiguous":
            dated = (
                [c for c in prov.candidates if _in_force(c, as_of)]
                if prov.reason == "several_texts" and as_of is not None
                else list(prov.candidates)
            )
            chosen = (
                _choose_by_words(catalogue, dated, quote, assessed_at)
                if quote is not None and dated
                else None
            )
            if chosen is not None:
                target_anchor_id = chosen.anchor_id
            elif prov.reason == "several_texts" and as_of is not None:
                in_force_cands = dated
                if len(in_force_cands) == 1:
                    target_anchor_id = in_force_cands[0].anchor_id
                elif len(in_force_cands) == 0:
                    return finish(
                        "not_in_force",
                        findings=_empty_findings(
                            currentness={
                                "state": "not_in_force",
                                "basis": "record",
                                "assessed_at": assessed_at.isoformat(),
                            }
                        ),
                    )
                else:
                    alts = [
                        {
                            "source_id": c.source_id,
                            "title": source_view.title,
                            "anchor_id": c.anchor_id,
                            "pinpoint": c.pinpoint,
                            "reason": "several_texts",
                        }
                        for c in in_force_cands
                    ]
                    return finish("ambiguous", alternatives=alts)
            else:
                alts = [
                    {
                        "source_id": c.source_id,
                        "title": source_view.title,
                        "anchor_id": c.anchor_id,
                        "pinpoint": c.pinpoint,
                        "reason": prov.reason or "ambiguous",
                    }
                    for c in prov.candidates
                ]
                return finish("ambiguous", alternatives=alts)

        # Row 6: part not found in held text
        if prov.status == "unresolved" and prov.reason == "not_found":
            nearest = None
            if prov.nearest is not None:
                nearest = {
                    "source_id": prov.nearest.source_id,
                    "title": source_view.title,
                    "anchor_id": prov.nearest.anchor_id,
                    "pinpoint": prov.nearest.pinpoint,
                }
            return finish("part_not_found", nearest=nearest)

        # Resolved single candidate
        if target_anchor_id is None:
            target_anchor_id = prov.candidates[0].anchor_id

    # --- Step 2: Read the part's words -------------------------------------------------------------
    assert target_anchor_id is not None
    row = catalogue.repository.node(target_anchor_id)
    if row is None:
        return finish("text_not_available")
    node = NodeViewRecord.model_validate(row)
    try:
        version, source, _ = catalogue.version(node.version_id)
    except SourceUnavailable:
        return finish("text_not_available")

    _enforce_allowed_source(source.id, allowed_source_ids)

    # Row 4 when anchor_id was sent alongside a non-matching language
    if language is not None and not _speaks(version, language):
        return finish("not_in_that_language")

    span_bytes, span_hash = catalogue._span(node, version)
    if len(span_bytes) > MAX_PART_BYTES:
        raise VerifyToolError(
            "result_too_large",
            "This part is larger than 256 KiB; name a smaller part.",
        )
    part_text = span_bytes.decode("utf-8")
    pinpoint_str = catalogue._pinpoint(node, source.document_type)

    part = {
        "source_id": source.id,
        "title": source.title,
        "version_id": version.id,
        "anchor_id": node.id,
        "pinpoint": pinpoint_str,
        "language": version.language,
        "span_hash": span_hash,
        "record_url": f"https://open.alvary.ai/sources/{source.id}",
        "official_url": str(version.canonical_url),
    }

    # --- Step 3: Match the quote -------------------------------------------------------------------
    if quote is None:
        quote_findings = {
            "quote": "not_given",
            "matched_text": None,
            "opening_text": None,
            "context_before": None,
            "context_after": None,
            "omitted": [],
        }
    else:
        quote_findings = match_quote(part_text, quote)

    # --- Step 4 & 5: Assess status and newer_text via Checker --------------------------------------
    if precomputed_check is not None:
        chk = precomputed_check
        newer_text = precomputed_newer_text
    else:
        checker = Checker(catalogue, today=today_utc, withdrawn=None)
        check_page = checker.check(
            (CheckItem(anchor_id=node.id, as_of=checker_as_of, jurisdiction=jurisdiction),)
        )
        chk = check_page.results[0]
        if chk.result == "newer_version":
            newer_text = {
                "version_id": chk.newer_version_id,
                "anchor_id": chk.newer_anchor_id,
                "same_words": chk.same_words,
            }
        else:
            newer_text = None

    currentness = (
        {
            "state": chk.currentness.state,
            "basis": chk.currentness.basis,
            "assessed_at": chk.currentness.assessed_at.isoformat(),
        }
        if chk.currentness is not None
        else None
    )
    applicability = (
        {
            "binds": chk.applicability.binds,
            "relation": chk.applicability.relation,
        }
        if chk.applicability is not None
        else None
    )

    findings = {
        **quote_findings,
        "currentness": currentness,
        "applicability": applicability,
        "newer_text": newer_text,
    }

    # --- Step 6: Decide status (Rows 7-13 in exact order) ------------------------------------------
    if currentness is not None and currentness["state"] == "not_in_force":
        status = "not_in_force"
    elif applicability is not None and applicability["binds"] == "not_bound":
        status = "does_not_bind"
    elif quote_findings["quote"] == "absent":
        status = "wording_differs"
    elif quote_findings["quote"] == "not_given":
        status = "located"
    elif quote_findings["quote"] == "with_omissions":
        status = "words_found_with_omissions"
    elif (
        quote_findings["quote"] in ("exact", "normalised")
        and currentness is not None
        and currentness["state"] == "in_force"
        and currentness["basis"] == "provision"
        and (applicability is None or applicability["binds"] == "bound")
    ):
        status = "matches"
    else:
        status = "words_found"

    return finish(status, part=part, findings=findings)
