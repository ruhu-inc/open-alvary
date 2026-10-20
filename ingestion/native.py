"""A PDF's own text layer as evidence for confidence-routed review (DIG-01, CPR-01).

A born-digital official text carries the words it prints in its text layer. Read with pypdf and
checked, that layer is a reading independent of every OCR reader: where it agrees with the reading
the reviewed units came from, and with the other readers, the page need not be proofread; it is
covered by the audit sample instead (workbench/routing.py, route ``native``).

Each page of the PDF is classed (profile PROFILE):
  native       a visible text layer, its glyphs mapped to real characters, holding the page's words
  scan         no text layer, an almost empty one, hidden OCR (invisible text over a picture), or a
               text layer under a full-page picture: someone else's OCR, not the printed text
  mixed        part of the page's words are not in the layer (a picture inside the page), or its
               words are there twice (its own text and a hidden OCR layer, or a layer that repeats
               itself more than any reading of the page does)
  corrupt      missing glyphs: replacement or private-use characters inside words, control
               characters, unmapped glyph codes, mojibake, or a layer that does not spell what the
               readers read (a font without a usable encoding)
  unsupported  pypdf cannot read the page

Words are compared letters and digits only, so typography, hyphenated line ends and words printed
together do not count as differences; running headers the reading keeps apart are taken out of the
layer first, and a run of words the layer draws elsewhere on the page (a margin note) is found there.

A ``native`` page passes when, besides, the layer agrees with the reading the units came from
(AGREEMENT_MIN), its words are in that reading's order (ORDER_MIN), no number and no negation word
differs between them, and an independent reading (an OCR of the page image, not the text layer
itself) agrees with it (CHECK_AGREEMENT_MIN). Where the units were read from the page image, the
evidence also says, for each flag of the reading, whether the layer reads as the units do there,
and how many words differ where no flag says so: routing resolves ordinary flags with the first,
and sends a page to full review on the second.

The layer is evidence only: it never changes a word of the reading, and its text is never joined
to an OCR reading of the same page. The evidence is bound to the PDF and to each page of the
reading it assessed (``bound``), so a reading prepared again sets it aside until it is assessed
again. A reading taken from the text layer itself (Rwanda's Gazette) has no independent reading of
its text-layer pages, so they cannot pass.

    python -m ingestion.native assess --staging DIR [--document ID] [--out DIR]

writes <id>.native.json beside each reading in DIR (or in --out).
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ContentStream

from ingestion.compare import NEGATIONS, TOKEN
from ingestion.ruled_tables import _times

PROFILE = "oa-native-1"
ROOT = Path(__file__).resolve().parents[1]

# Thresholds (DIG-01), recorded with the evidence so that it says what it was measured against.
SCAN_MAX_WORDS = 5  # a text layer with fewer words than this is no text layer
HIDDEN_SHARE = 0.8  # glyphs drawn invisibly (render mode 3 or 7), as a share of all: hidden OCR
MIXED_HIDDEN_SHARE = 0.2  # at least this share invisible, the rest visible: the text is there twice
SCAN_IMAGE_COVER = 0.85  # a picture covering this share of the page, drawn over its text: a scan
CORRUPT_RECALL = 0.5  # fewer of the readers' words than this anywhere in the layer: it is garbled
NATIVE_RECALL = 0.9  # fewer than this (but more than CORRUPT_RECALL): part of the page is a picture
DUPLICATION_EXCESS = 0.5  # the share of 8-word runs occurring twice, more than in the readings: doubled
AGREEMENT_MIN = 0.98  # agreement with the reading the units came from
ORDER_MIN = 0.85  # of the words both have, the share in that reading's order (margin notes aside)
CHECK_AGREEMENT_MIN = 0.95  # agreement with at least one independent reading
MOVED_MIN_LETTERS = 8  # a run of words found elsewhere on the page counts as moved from this long
FURNITURE_REACH = 5  # a running header is looked for this many words from the top or the foot
FURNITURE_SHAPE_MIN = 4  # another page's header is looked for on this one when it has as many words
THRESHOLDS = {
    "scan_max_words": SCAN_MAX_WORDS,
    "hidden_share": HIDDEN_SHARE,
    "mixed_hidden_share": MIXED_HIDDEN_SHARE,
    "scan_image_cover": SCAN_IMAGE_COVER,
    "corrupt_recall": CORRUPT_RECALL,
    "native_recall": NATIVE_RECALL,
    "duplication_excess": DUPLICATION_EXCESS,
    "agreement_min": AGREEMENT_MIN,
    "order_min": ORDER_MIN,
    "check_agreement_min": CHECK_AGREEMENT_MIN,
    "moved_min_letters": MOVED_MIN_LETTERS,
    "furniture_reach": FURNITURE_REACH,
    "furniture_shape_min": FURNITURE_SHAPE_MIN,
}

WORD = re.compile(r"\d+|[^\W\d_]+")  # letters or digits; a number and a word printed together split
GLUED = re.compile(r"(?<=[a-z])(?=[A-Z][a-z])")
CID = re.compile(r"\(cid:\d+\)")
MOJIBAKE = re.compile("Ã[\u0080-\u00bf]|Â[\u0080-\u00bf]|â€|ï¿½")
INVISIBLE = {3, 7}  # text render modes that paint nothing


def words(text: str) -> list[str]:
    """Comparison words: letters or digits, lower case, typography folded (ligatures, the Naira sign
    read as N); punctuation, hyphens and spacing play no part, and two words a text layer printed
    together ("nominationWithdrawal") are two."""
    text = GLUED.sub(" ", unicodedata.normalize("NFKC", text))
    return WORD.findall(text.replace("₦", "N").lower())


# --- what a page draws ----------------------------------------------------------------------------


def _shown(operands) -> int:
    """How many glyph codes a text-showing operator draws (bytes; two per glyph in a CID font)."""
    count = 0
    for operand in operands:
        if isinstance(operand, list):
            count += _shown(operand)
        elif isinstance(operand, str | bytes):
            count += len(getattr(operand, "original_bytes", operand))
    return count


def marks(page) -> dict:
    """What a pypdf page draws: glyphs shown visibly and invisibly, the share of the page its
    pictures cover, and whether a page-sized picture is drawn over most of the visible text."""
    area = float(page.mediabox.width) * float(page.mediabox.height) or 1.0
    found = {"visible": 0, "invisible": 0, "image_cover": 0.0}
    before = [0]  # visible glyphs drawn before the last page-sized picture

    def walk(stream, resources, matrix, depth):
        mode, saved = 0, []
        xobjects = resources.get("/XObject") if resources else None
        xobjects = xobjects.get_object() if xobjects is not None else {}
        for operands, operator in ContentStream(stream, page.pdf).operations:
            if operator == b"q":
                saved.append((matrix, mode))
            elif operator == b"Q" and saved:
                matrix, mode = saved.pop()
            elif operator == b"cm" and len(operands) == 6:
                matrix = _times([float(v) for v in operands], matrix)
            elif operator == b"Tr" and operands:
                mode = int(operands[0])
            elif operator in (b"Tj", b"TJ", b"'", b'"'):
                found["invisible" if mode in INVISIBLE else "visible"] += _shown(operands)
            elif operator == b"INLINE IMAGE":
                picture(matrix)
            elif operator == b"Do" and operands and operands[0] in xobjects:
                xobject = xobjects[operands[0]].get_object()
                if xobject.get("/Subtype") == "/Image":
                    picture(matrix)
                elif xobject.get("/Subtype") == "/Form" and depth < 4:
                    inner = [float(v) for v in xobject.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    walk(xobject, xobject.get("/Resources") or resources, _times(inner, matrix), depth + 1)

    def picture(matrix):
        cover = abs(matrix[0] * matrix[3] - matrix[1] * matrix[2]) / area
        found["image_cover"] = min(1.0, found["image_cover"] + cover)
        if cover >= SCAN_IMAGE_COVER:
            before[0] = found["visible"]

    contents = page.get_contents()
    if contents is not None:
        walk(contents, page.get("/Resources"), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], 0)
    found["image_cover"] = round(found["image_cover"], 4)
    # A picture drawn after most of the visible text hides it: the text is OCR laid under a scan.
    found["image_over_text"] = bool(found["visible"]) and before[0] >= found["visible"] / 2
    return found


def layer(raw: bytes, numbers: list[int] | None = None) -> dict[int, dict]:
    """The text layer of each page (numbered from 1; these only, if given) as pypdf reads it, with
    what the page draws; or why it cannot be read."""
    try:
        pages = PdfReader(BytesIO(raw)).pages
        count = len(pages)
    except Exception as error:  # noqa: BLE001 - an unreadable PDF has no usable layer
        raise ValueError(f"pypdf cannot read the PDF: {error}") from error
    out = {}
    for number in numbers if numbers is not None else range(1, count + 1):
        if not 1 <= number <= count:
            out[number] = {"text": None, "error": "not in the PDF"}
            continue
        try:
            page = pages[number - 1]
            out[number] = {"text": page.extract_text() or "", **marks(page)}
        except Exception as error:  # noqa: BLE001 - one unreadable page is recorded, not fatal
            out[number] = {"text": None, "error": f"{type(error).__name__}: {error}"[:200]}
    return out


# --- what the layer says --------------------------------------------------------------------------


def glyphs(text: str) -> dict:
    """Characters that are not text: replacement characters, private-use characters inside words
    (a glyph without a meaning; one standing alone is a symbol such as a bullet), control
    characters, unassigned code points, unmapped glyph codes ("(cid:12)") and mojibake."""
    counts = {"replacement": text.count("\ufffd"), "private_use": 0, "symbols": 0, "control": 0}
    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if category == "Co":
            near = text[max(0, index - 1) : index + 2]
            alone = not any(c.isalnum() for c in near if c != char)
            counts["symbols" if alone else "private_use"] += 1
        elif category in ("Cc", "Cn", "Cs") and char not in "\t\n\r\f":
            counts["control"] += 1
    counts["cid"] = len(CID.findall(text))
    counts["mojibake"] = len(MOJIBAKE.findall(text))
    return counts


def duplication(found: list[str], size: int = 8) -> float:
    """The share of the layer's runs of `size` words that occur more than once."""
    runs = Counter(tuple(found[i : i + size]) for i in range(len(found) - size + 1))
    total = sum(runs.values())
    return round(sum(n for n in runs.values() if n > 1) / total, 4) if total else 0.0


def _shape(found: list[str]) -> tuple:
    """A furniture line's words with its numbers only counted: running headers differ by page number."""
    return tuple(sorted(Counter(w for w in found if not w.isdigit()).items())), sum(
        w.isdigit() for w in found
    )


def strip_furniture(found: list[str], own: list[str], document: list[str] = ()) -> list[str]:
    """The layer's words without the running headers and page numbers the reading keeps apart. A
    furniture line is looked for at the top and the foot of the layer (within FURNITURE_REACH words
    of them), its words in any order: the page's own lines exactly, and once, the running header
    of the document's other pages with any page number (where the reading kept only part of it)."""
    found = list(found)
    shapes = {_shape(ws): ws for ws in map(words, document) if len(ws) >= FURNITURE_SHAPE_MIN}
    lines = [(words(line), True) for line in own] + [(ws, False) for ws in shapes.values()]
    removed: list[Counter] = []
    header = False  # one running header a page is taken from the other pages
    for line, exact in sorted(lines, key=lambda line: -len(line[0])):  # the longest first
        size, last = len(line), len(found) - len(line)
        if not size or last < 0 or (not exact and header):
            continue
        if exact and any(not Counter(line) - r for r in removed):
            continue  # removed already, with a longer line
        reach = size + FURNITURE_REACH
        for start in sorted({*range(min(reach, last + 1)), *range(max(0, last + 1 - reach), last + 1)}):
            window = found[start : start + size]
            if Counter(window) == Counter(line) if exact else _shape(window) == _shape(line):
                removed.append(Counter(window))
                header = header or not exact
                del found[start : start + size]
                break
    return found


def _in_order(native: list[str], reading: list[str], keep: list[int], against: list[int]):
    """The layer's words `keep` against the reading's words `against`, in order: the reading words
    confirmed, the layer words used, and the runs of layer words left over with the reading
    boundary they stand at. A word split differently (a hyphenated line end, a page number printed
    against a word) counts as the same word."""
    a, b = [native[i] for i in keep], [reading[j] for j in against]
    confirmed, used, left = set(), set(), []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and "".join(a[i1:i2]) == "".join(b[j1:j2])):
            confirmed.update(against[j1:j2])
            used.update(keep[i1:i2])
        elif i2 > i1:
            left.append((keep[i1:i2], against[j1] if j1 < len(against) else len(reading)))
    return confirmed, used, left


def _runs(confirmed: list[bool], free: list[bool]) -> list[tuple[int, int]]:
    """Runs of reading words not confirmed, split where margin notes start or end."""
    runs, start = [], None
    for j in range(len(confirmed) + 1):
        if start is not None and (j == len(confirmed) or confirmed[j] or free[j] != free[start]):
            runs.append((start, j))
            start = None
        if j < len(confirmed) and not confirmed[j] and start is None:
            start = j
    return runs


def _places(found: list[str], letters: str, taken: set[int]) -> list[tuple[int, int]]:
    """Where these letters are printed in a list of words, from a word start to a word end."""
    out = []
    for at in range(len(found)):
        joined, end = "", at
        while end < len(found) and end not in taken and len(joined) < len(letters):
            joined, end = joined + found[end], end + 1
        if joined == letters:
            out.append((at, end))
    return out


def align(native: list[str], reading: list[str], free: list[bool] | None = None) -> dict:
    """The layer's words against a reading's: in order first; then a run the order left over that
    is printed once elsewhere in the layer, mostly among words the order did not use (a margin note
    drawn after the text, say), is moved there and the order taken again without it; last, runs
    still left found among the layer's words still unused. Margin notes the reading places beside
    their unit (``free``) are expected elsewhere and do not count against the order.

    Returns, per reading word, whether the layer confirms it; the words the layer has that the
    reading lacks, by the boundary of the reading where they stand; and the measures."""
    free = free or [False] * len(reading)
    first, used, _ = _in_order(native, reading, list(range(len(native))), list(range(len(reading))))
    taken, moved = set(), set()
    for j1, j2 in _runs([j in first for j in range(len(reading))], free):
        letters = "".join(reading[j1:j2])
        if len(letters) < MOVED_MIN_LETTERS and not all(free[j1:j2]):
            continue
        places = _places(native, letters, taken)
        if len(places) != 1 or len(_places(reading, letters, set())) != 1:
            continue  # not printed once in each: which is which cannot be told
        at, end = places[0]
        if 2 * sum(i not in used for i in range(at, end)) >= end - at:
            taken.update(range(at, end))
            moved.update(range(j1, j2))
    keep = [i for i in range(len(native)) if i not in taken]
    ordered, used, left = _in_order(native, reading, keep, [j for j in range(len(reading)) if j not in moved])
    used |= taken
    confirmed = [j in ordered or j in moved for j in range(len(reading))]
    pool = [[native[i], i] for run, _ in left for i in run]
    for j1, j2 in _runs(confirmed, free):
        letters = "".join(reading[j1:j2])
        if len(letters) < MOVED_MIN_LETTERS and not all(free[j1:j2]):
            continue
        spent = {n for n, (word, _) in enumerate(pool) if word is None}
        for at, end in _places([word or "" for word, _ in pool], letters, spent)[:1]:
            for entry in pool[at:end]:
                used.add(entry[1])
                entry[0] = None  # a word of the layer explains one run only
            confirmed[j1:j2] = [True] * (j2 - j1)
            moved.update(range(j1, j2))
    extra_at: dict[int, list[str]] = {}
    for run, j in left:
        extra_at.setdefault(j, []).extend(native[i] for i in run if i not in used)
    extra_at = {j: found for j, found in extra_at.items() if found}
    in_order = len(ordered)
    shifted = sum(1 for j in moved if not free[j])
    total = len(native) + len(reading)
    return {
        "confirmed": confirmed,
        "extra_at": extra_at,
        "agreement": round((sum(confirmed) + len(used)) / total, 4) if total else 1.0,
        "order": round(in_order / (in_order + shifted), 4) if in_order + shifted else 1.0,
        "missing": len(reading) - sum(confirmed),
        "extra": len(native) - len(used),
        "unmatched": (
            [w for w, ok in zip(reading, confirmed, strict=True) if not ok],
            [w for i, w in enumerate(native) if i not in used],
        ),  # fmt: skip
    }


def notes_mask(page: dict, reading: list[str]) -> list[bool]:
    """Which words of the page's text are margin notes (placed beside their unit in the reading, and
    wherever the PDF drew them in the layer)."""
    mask, at = [False] * len(reading), 0
    for node in page.get("nodes") or []:
        for note in node.get("notes") or []:
            run = words(note)
            for start in range(at, len(reading) - len(run) + 1):
                if run and reading[start : start + len(run)] == run:
                    mask[start : start + len(run)] = [True] * len(run)
                    at = start + len(run)
                    break
    return mask


def against_flags(page: dict, aligned: dict) -> dict:
    """For each flag of the page (by index), whether the layer reads as the units do at its words:
    every word of the flagged span confirmed and nothing of the layer's own between them; for words
    only the check readers have, nothing of the layer's own where they would stand. And how many
    words differ between the layer and the units where no flag covers them: a word the other side
    has elsewhere among its own unmatched words was only placed differently, and does not count."""
    tokens = TOKEN.findall(page.get("text") or "")
    starts, at = [], 0
    for token in tokens:
        starts.append(at)
        at += len(words(token))
    starts.append(at)
    confirmed, extra_at = aligned["confirmed"], aligned["extra_at"]
    supported, unsupported, covered, places = [], [], set(), set()
    for index, flag in enumerate(page.get("flags") or []):
        first, last = (min(max(int(t), 0), len(tokens)) for t in (flag.get("tokens") or [0, 0])[:2])
        start, end = starts[first], starts[max(first, last)]
        covered.update(range(start, end))
        places.update(range(start, end + 1))
        if end > start:
            ok = all(confirmed[start:end]) and not any(start < b < end for b in extra_at)
        else:
            ok = (
                start not in extra_at
                and (start == 0 or confirmed[start - 1])
                and (start == len(confirmed) or confirmed[start])
            )
        (supported if ok else unsupported).append(index)
    units, layer = (Counter(side) for side in aligned["unmatched"])
    reading = words(page.get("text") or "")
    alone_units = Counter(reading[j] for j, ok in enumerate(confirmed) if not ok and j not in covered)
    alone_layer = Counter(w for b, found in extra_at.items() if b not in places for w in found)
    unflagged = (alone_units - layer).total() + (alone_layer - units).total()
    return {"supported": supported, "unsupported": unsupported, "unflagged_differences": unflagged}


def bound(page: dict) -> str:
    """What the evidence for a page is bound to: the reading's words, units and flags there."""
    held = {key: page.get(key) for key in ("nodes", "text", "flags")}
    return hashlib.sha256(
        json.dumps(held, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _disagree(aligned: dict, kind) -> str | None:
    """Numbers or negation words the layer and the units do not share where they stand, if any."""
    units, layer = (", ".join([w for w in side if kind(w)][:5]) for side in aligned["unmatched"])
    if not units and not layer:
        return None
    return f"the text layer has {layer or 'nothing'} where the units have {units or 'nothing'}"


def assess_page(drawn: dict, page: dict, primary: str, furniture: list[str] = ()) -> dict:
    """One page's class and whether its text layer passed, with the measures and the reasons.
    `drawn` is the page from layer(); `primary` names the reader the reading's units came from
    ("text-layer" when they were taken from the layer itself, which is then no independent check);
    `furniture` is the running headers of the document's pages."""
    evidence = {"profile": PROFILE, "bound": bound(page)}
    if drawn.get("text") is None:
        reason = f"the page could not be read: {drawn.get('error') or 'not in the PDF'}"
        return {**evidence, "class": "unsupported", "agreement": 0.0, "numbers_agree": False,
                "negations_agree": False, "passed": False, "reasons": [reason]}  # fmt: skip
    text = drawn["text"]
    native = strip_furniture(words(text), page.get("furniture") or [], furniture)
    reading = words(page.get("text") or "")
    checks = {name: words(t) for name, t in (page.get("checks") or {}).items() if isinstance(t, str) and t}
    counted = Counter(native)
    recall = max(
        (sum((Counter(r) & counted).values()) / len(r) for r in [reading, *checks.values()] if r), default=1.0
    )
    found, repeated = glyphs(text), duplication(native)
    # A law repeats its own phrases; a layer printed twice repeats more than any reading of the page.
    excess = repeated - max((duplication(r) for r in [reading, *checks.values()] if r), default=0.0)
    kind, why = classify(drawn, len(native), found, recall, excess)
    reasons = [why] if why else []
    aligned = align(native, reading, notes_mask(page, reading))
    measured = ("agreement", "order", "missing", "extra")
    readers = {
        name: {k: v for k, v in align(native, ws).items() if k in measured} for name, ws in checks.items()
    }
    if primary != "text-layer":  # the units were read from the page image: an independent reading
        readers[primary] = {k: aligned[k] for k in measured}
    numbers = _disagree(aligned, str.isdigit)
    negations = _disagree(aligned, NEGATIONS.__contains__)
    numbers_agree, negations_agree = numbers is None, negations is None
    if kind == "native":
        if aligned["agreement"] < AGREEMENT_MIN:
            reasons.append(
                f"agrees with the reading the units came from on {aligned['agreement']:.1%} "
                f"(at least {AGREEMENT_MIN:.0%} needed): {aligned['missing']} of its words not in the "
                f"text layer, {aligned['extra']} of the layer's not in it"
            )
        if aligned["order"] < ORDER_MIN:
            reasons.append(f"only {aligned['order']:.0%} of the shared words are in the reading's order")
        if numbers:
            reasons.append(f"the numbers differ: {numbers}")
        if negations:
            reasons.append(f"the negations differ: {negations}")
        best = max((m["agreement"] for m in readers.values()), default=None)
        if best is None:
            reasons.append(
                "no independent reading of the page (an OCR of its image) to check the layer against"
            )
        elif best < CHECK_AGREEMENT_MIN:
            reasons.append(f"no independent reading agrees with the layer (best {best:.1%})")
    passed = kind == "native" and not reasons
    if passed:
        reasons.append("a clean text layer that agrees with the readings, numbers and negations exactly")
    return {
        **evidence,
        "class": kind,
        "agreement": aligned["agreement"],
        "order": aligned["order"],
        "numbers_agree": numbers_agree,
        "negations_agree": negations_agree,
        "passed": passed,
        "reasons": reasons,
        "words": len(native),
        "differences": {"missing": aligned["missing"], "extra": aligned["extra"]},
        "readers": readers,
        "recall": round(recall, 4),
        "duplication": repeated,
        "glyphs": found,
        "drawn": {k: drawn.get(k) for k in ("visible", "invisible", "image_cover", "image_over_text")},
        # Against the units only where they were read from the page image: a reading taken from the
        # layer itself cannot be checked against it.
        "flags": against_flags(page, aligned) if kind == "native" and primary != "text-layer" else None,
    }


def classify(drawn: dict, size: int, found: dict, recall: float, excess: float) -> tuple[str, str | None]:
    """A page's class from what it draws and what its layer holds, and why when it is not native."""
    shown = drawn.get("visible", 0) + drawn.get("invisible", 0)
    hidden = drawn.get("invisible", 0) / shown if shown else 0.0
    missing = {name: n for name, n in found.items() if n and name != "symbols"}
    if size < SCAN_MAX_WORDS:
        return "scan", f"no text layer to use ({size} words)"
    if hidden >= HIDDEN_SHARE:
        return "scan", f"the text layer is hidden OCR: {hidden:.0%} of its glyphs are invisible"
    if hidden >= MIXED_HIDDEN_SHARE:
        return "mixed", f"the page has its own text and a hidden OCR layer ({hidden:.0%} of glyphs invisible)"
    if drawn.get("image_over_text"):
        return "scan", "a page-sized picture is drawn over the text layer: the layer is OCR under a scan"
    if missing:
        return "corrupt", "missing glyphs: " + ", ".join(
            f"{n} {k.replace('_', ' ')}" for k, n in missing.items()
        )
    if recall < CORRUPT_RECALL:
        return "corrupt", f"the text layer does not spell what the readers read ({recall:.0%} of their words)"
    if excess >= DUPLICATION_EXCESS:
        return (
            "mixed",
            f"the text layer repeats itself: {excess:.0%} more of its 8-word runs twice than the readers",
        )
    if recall < NATIVE_RECALL:
        return (
            "mixed",
            f"only {recall:.0%} of the readers' words are in the text layer: part of the page is a picture",
        )
    return "native", None


def assess(raw: bytes, reading: dict) -> dict:
    """The evidence for every page of a reading, from the PDF it was made from."""
    sha = hashlib.sha256(raw).hexdigest()
    if reading.get("input_sha256") and reading["input_sha256"] != sha:
        raise ValueError("the PDF is not the one the reading was made from")
    drawn = layer(raw, [page["page"] for page in reading["pages"]])
    primary = (reading.get("primary") or {}).get("reader") or "unknown"
    furniture = [line for page in reading["pages"] for line in page.get("furniture") or []]
    pages = {}
    for page in reading["pages"]:
        number = page["page"]
        pages[str(number)] = assess_page(drawn[number], page, primary, furniture)
    return {
        "profile": PROFILE,
        "input_sha256": sha,
        "primary": primary,
        "assessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": f"pypdf {__import__('pypdf').__version__}",
        "thresholds": THRESHOLDS,
        "pages": pages,
    }


def attach(reading: dict, evidence: dict | None) -> dict:
    """The reading with each page's evidence as page["native"], where the evidence is of this
    profile, from this PDF, and bound to the page as it now reads."""
    if not evidence or evidence.get("profile") != PROFILE:
        return reading
    if evidence.get("input_sha256") != reading.get("input_sha256"):
        return reading
    found = evidence.get("pages") or {}
    for page in reading["pages"]:
        page.pop("native", None)
        held = found.get(str(page["page"]))
        if held and held.get("bound") == bound(page):
            page["native"] = held
    return reading


def summary(evidence: dict) -> dict:
    pages = evidence["pages"].values()
    return {
        "pages": len(pages),
        "classes": dict(Counter(p["class"] for p in pages)),
        "passed": sum(p["passed"] for p in pages),
    }


def _outside_repository(folder: Path) -> bool:
    """Evidence describes private originals: written in private staging or outside the repository."""
    folder = folder.resolve()
    return not folder.is_relative_to(ROOT) or folder.is_relative_to(ROOT / "private-staging")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("assess", help="assess the text layer of each document with a reading")
    run.add_argument("--staging", required=True, type=Path, help="the folder of readings and their PDFs")
    run.add_argument("--document", help="only this document")
    run.add_argument("--out", type=Path, help="write the evidence here instead of beside the readings")
    args = parser.parse_args(argv)
    out = args.out or args.staging
    if not _outside_repository(out):
        parser.error(
            "evidence about private originals is written in private staging or outside the repository"
        )
    out.mkdir(parents=True, exist_ok=True)
    names = [args.document] if args.document else sorted(
        p.name[: -len(".reading.json")] for p in args.staging.glob("*.reading.json")
    )  # fmt: skip
    for name in names:
        pdf = args.staging / f"{name}.pdf"
        if not pdf.exists():
            print(f"{name}: no PDF beside the reading", file=sys.stderr)
            continue
        reading = json.loads((args.staging / f"{name}.reading.json").read_text())
        try:
            evidence = assess(pdf.read_bytes(), reading)
        except ValueError as error:
            print(f"{name}: {error}", file=sys.stderr)
            continue
        (out / f"{name}.native.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n")
        print(json.dumps({"document": name, **summary(evidence)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
