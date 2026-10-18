"""Pinpoints within a text (API-04 provision lookup): how a part is cited from its printed numbers,
"s. 23(1)(a)", "art. 12", "First Schedule, para. 3", a definition with its section; and the reverse, a
cited pinpoint read into the same key, so the citation an anchor gives resolves back to that anchor.

A key compares numbers as printed, case aside ("23A" is "23a"); schedule and part numbers compare as
numbers ("Second Schedule", "Schedule 2" and "Schedule II" are one schedule). Paragraph letters are
never read as Roman numerals: "(i)" is "i" wherever it is printed.
"""

import re
from typing import Protocol

ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth".split()
_ROMAN = re.compile(r"^[ivxlc]+$")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_WORD_ONE = {"mbere", "premier", "one", "first"}
# Leads of a numbered provision: sections of an Act, regulations and rules of an instrument made under
# one, all keyed alike; articles apart.
_SECTION_LEADS = r"ss?|secs?|sects?|sections?|§§?|regs?|regulations?|r|rr|rules?"
_ARTICLE_LEADS = r"arts?|articles?|ingingo(?:\s+ya)?"
_SUBS = r"((?:\(\s*[0-9a-z]{1,5}\s*\)\s*)*)"
_QUOTE = r"[\"“”'‘’]"
SECTION = re.compile(
    rf"^(?:{_SECTION_LEADS})\.?\s*(\d{{1,4}}[a-z]{{0,2}})\.?\s*{_SUBS}"
    rf"(?:,?\s*(?:definition\s+of\s+)?{_QUOTE}\s*(.+?)\s*{_QUOTE})?$",
    re.I,
)
ARTICLE = re.compile(
    rf"^(?:{_ARTICLE_LEADS})\.?\s*(\d{{1,4}}|[ivxlc]{{1,8}}|mbere|premier|one|first)(?:er|ère)?\.?\s*{_SUBS}$",
    re.I,
)
SCHEDULE = re.compile(
    rf"^(?:({'|'.join(ORDINALS)}|\d{{1,2}}(?:st|nd|rd|th))\s+schedule|(?:schedule|sch)\.?\s*(\d{{1,2}}|[ivxlc]{{1,6}})?)"
    r"\s*(?:,?\s*(?:part|pt)\.?\s*(\d{1,2}|[ivxlc]{1,6}))?"
    rf"\s*(?:,?\s*(?:paragraphs?|paras?|p)\.?\s*(\d{{1,3}}[a-z]?)\.?\s*{_SUBS})?$",
    re.I,
)
# Where a pinpoint may begin within a citation: "NDPA s. 23(1)(a)", "ISA 2025, First Schedule, para. 3".
_STARTS = re.compile(
    rf"(?:^|(?<=[\s,;(]))(?:{_SECTION_LEADS}|{_ARTICLE_LEADS}|(?:{'|'.join(ORDINALS)}|\d{{1,2}}(?:st|nd|rd|th))\s+schedule"
    r"|schedule|sch)(?=[\s.,\d(]|$)",
    re.I,
)


class Part(Protocol):
    kind: str
    label: str | None
    locator: str


def _roman(value: str) -> int:
    values = [_ROMAN_VALUES[c] for c in value]
    return sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))


def _printed(label: str | None) -> str:
    """A printed number without its dot or brackets: "23A." → "23A", "(1)" → "1"."""
    return (label or "").strip().strip(".()").strip()


def _plain(label: str | None) -> str:
    return re.sub(r"\s+", "", _printed(label)).casefold()


def _ordinal(value: str | None) -> str:
    """A schedule or part number as a number: "second", "2nd", "II" and "2" are all "2"."""
    value = (value or "").strip().casefold()
    if value in ORDINALS:
        return str(ORDINALS.index(value) + 1)
    if re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)", value):
        return str(int(value[:-2]))
    if _ROMAN.fullmatch(value):
        return str(_roman(value))
    return value.lstrip("0") or value


def _token_number(locator: str, token: str) -> str:
    """The number in a node's own locator token ("art_12", "sch_ii"), without the suffix a repeated one
    is published with ("art_12_2")."""
    own = locator.rsplit("__", 1)[-1]
    found = re.match(rf"{token}_([0-9]+|[ivxlc]+)", own)
    return found.group(1) if found else ""


def _schedule_title(label: str | None) -> str:
    words = (label or "Schedule").split()
    return " ".join(
        w.capitalize()
        if w.casefold() in (*ORDINALS, "schedule")
        else w.upper()
        if _ROMAN.fullmatch(w.casefold())
        else w
        for w in words
    )


def cite(chain: list[Part], document_type: str | None = None) -> tuple[tuple[str, ...], str] | None:
    """The key and printed pinpoint of the last part of ``chain`` (outermost first), or None for a part
    no pinpoint names (the document, a contents list, a part of an Act)."""
    key: list[str] = []
    shown = ""
    schedule = False
    lead = "reg. " if document_type == "regulation" else "s. "
    for part in chain:
        if part.kind == "schedule":
            schedule, key = True, [f"sch:{_ordinal(_token_number(part.locator, 'sch')) or '1'}"]
            shown = _schedule_title(part.label)
        elif schedule and part.kind == "part" and part.label:
            number = re.sub(r"^part\s+", "", _printed(part.label), flags=re.I)
            key.append(f"part:{_ordinal(number)}")
            shown += f", Part {number.upper() if _ROMAN.fullmatch(number.casefold()) else number}"
        elif part.kind == "section" and part.label and not schedule:
            key, shown = [f"s:{_plain(part.label)}"], f"{lead}{_printed(part.label)}"
        elif part.kind == "article" and part.label:
            number = _token_number(part.locator, "art")
            key, shown = [f"art:{number}"], f"art. {number}"
        elif part.kind in ("subsection", "paragraph", "subparagraph") and part.label:
            if schedule and not any(k.startswith("para:") for k in key):
                key.append(f"para:{_plain(part.label)}")
                shown += f", para. {_printed(part.label)}"
            elif key:
                key.append(_plain(part.label))
                shown += f"({_printed(part.label)})"
            else:
                return None  # a numbered paragraph outside any section
        elif part.kind == "definition" and part.label and key:
            key.append(f"def:{' '.join(part.label.split()).casefold()}")
            shown += f", “{part.label}”"
    return (tuple(key), shown) if key else None


def read(pinpoint: str) -> tuple[str, ...] | None:
    """The key of a cited pinpoint, or None when it is not one this module reads."""
    value = " ".join(pinpoint.split()).strip(" ,;")
    subs = lambda group: [_plain(s) for s in re.findall(r"\(\s*([0-9a-z]{1,5})\s*\)", group or "", re.I)]  # noqa: E731
    found = SECTION.match(value)
    if found:
        key = [f"s:{_plain(found.group(1))}", *subs(found.group(2))]
        if found.group(3):
            key.append(f"def:{' '.join(found.group(3).split()).casefold()}")
        return tuple(key)
    found = ARTICLE.match(value)
    if found:
        number = found.group(1).casefold()
        number = "1" if number in _WORD_ONE else str(_roman(number)) if _ROMAN.fullmatch(number) else number
        return (f"art:{number.lstrip('0') or number}", *subs(found.group(2)))
    found = SCHEDULE.match(value)
    if found:
        key = [f"sch:{_ordinal(found.group(1) or found.group(2)) or '1'}"]
        if found.group(3):
            key.append(f"part:{_ordinal(found.group(3))}")
        if found.group(4):
            key += [f"para:{_plain(found.group(4))}", *subs(found.group(5))]
        return tuple(key)
    return None


def split(citation: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Each way to read a citation as a work and a pinpoint within it, longest pinpoint first:
    "NDPA s. 23(1)(a)" → ("NDPA", "s. 23(1)(a)", key); "section 23 of the NDPA" likewise."""
    value = " ".join(citation.split())
    found = []
    for start in _STARTS.finditer(value):
        work, pin = value[: start.start()].strip(" ,;("), value[start.start() :]
        key = read(pin)
        if work and key:
            found.append((work, pin.strip(" ,;"), key))
    for of in re.finditer(r"\s+of\s+(?:the\s+)?", value, re.I):
        pin, work = value[: of.start()], value[of.end() :].strip(" ,;")
        key = read(pin)
        if work and key:
            found.append((work, pin.strip(" ,;"), key))
    return found
