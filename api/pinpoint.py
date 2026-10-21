"""Pinpoints within a text (API-04 provision lookup): how a part is cited from its printed numbers,
"s. 23(1)(a)", "art. 12", "First Schedule, para. 3", "Part V", a definition with its section; and the reverse, a
cited pinpoint read into the same key, so the citation an anchor gives resolves back to that anchor.

A key compares numbers as printed, case aside ("23A" is "23a"); schedule and part numbers compare as
numbers ("Second Schedule", "Schedule 2" and "Schedule II" are one schedule). Paragraph letters are
never read as Roman numerals: "(i)" is "i" wherever it is printed. An article's alinéa, "article 10,
alinéa 2" or "art. 10 al. 2", is keyed as "article 10(2)" is, and an alinéa node (art_10__al_2, which has
no printed number) is cited "art. 10, al. 2". An article inserted after another keeps its printed number
("art. 853-1", "art. 626-1-1-1"). A work may follow its pinpoint after "of" or the French "de", "du",
"de la", "de l'" or "des": "article 10, alinéa 2, de l'Acte uniforme …".

A judgment's paragraph (judgment-paragraphs-1) is cited by its number in brackets after the neutral
citation, "[2019] NGSC 4 [12]" or "[2019] NGSC 4 at [12]", or "[2019] NGSC 4, para. 12"; a paragraph
of a later opinion that numbers its own paragraphs is "op. 2 [4]". "para. 12" alone is read only
after a neutral citation: in an Act it would be a schedule's paragraph.
"""

import re
import unicodedata
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
_ALINEA = r"(?:,?\s*(?:alin[ée]a|al)\.?\s*(\d{1,3}|premier)(?:er)?\.?)?"  # "alinéa 2", "al. 2", "alinéa 1er"
# Between a pinpoint and the work it is in: "s. 23 of the NDPA", "article 10 de l'Acte uniforme …". The
# French words in lower case only, as they are written, so "s. 5 De Beers Act" keeps its title whole.
_OF = r"(?:of\s+(?:the\s+)?|(?-i:de\s+la\s+|de\s+l['’]\s*|des\s+|du\s+|de\s+))"
_QUOTE = r"[\"“”'‘’]"
SECTION = re.compile(
    rf"^(?:{_SECTION_LEADS})\.?\s*(\d{{1,4}}[a-z]{{0,2}})\.?\s*{_SUBS}"
    rf"(?:,?\s*(?:definition\s+of\s+)?{_QUOTE}\s*(.+?)\s*{_QUOTE})?$",
    re.I,
)
ARTICLE = re.compile(
    rf"^(?:{_ARTICLE_LEADS})\.?\s*(\d{{1,4}}(?:-\d{{1,3}}){{0,4}}|[ivxlc]{{1,8}}|mbere|premier|one|first)(?:er|ère)?\.?\s*"
    rf"{_SUBS}{_ALINEA}$",
    re.I,
)
SCHEDULE = re.compile(
    rf"^(?:({'|'.join(ORDINALS)}|\d{{1,2}}(?:st|nd|rd|th))\s+schedule|(?:schedule|sch)\.?\s*(\d{{1,2}}|[ivxlc]{{1,6}})?)"
    r"\s*(?:,?\s*(?:part|pt)\.?\s*(\d{1,2}|[ivxlc]{1,6}))?"
    rf"\s*(?:,?\s*(?:paragraphs?|paras?|p)\.?\s*(\d{{1,3}}[a-z]?)\.?\s*{_SUBS})?$",
    re.I,
)
# A Part or Chapter of an Act outside its schedules: "Part V", "Pt. 5", "Chapter 3", "Chap. III".
CONTAINER = re.compile(
    rf"^(part|pt|chapter|chap|ch)\.?\s*(\d{{1,2}}|[ivxlc]{{1,6}}|{'|'.join(ORDINALS)})$",
    re.I,
)
# A neutral citation, "[2019] NGSC 4", "[2020] EWCA Civ 123", "[2020] EWHC 123 (Ch)", and a judgment's
# paragraph after it: "[12]", "at [12]", "op. 2 [4]", "para. 12" (group 3, read only after a citation).
NEUTRAL = re.compile(r"\[\d{4}\]\s+[a-z]{2,16}(?:\s+[a-z]{2,11})?\s+\d{1,6}(?:\s*\([a-z]{1,20}\))?", re.I)
PARAGRAPH = re.compile(
    r"^(?:(?:op|opinion)\.?\s*(\d{1,2})\s*,?\s*)?(?:at\s+)?"
    r"(?:\[\s*(\d{1,4}[a-z]?)\s*\]|(?:paras?|paragraphs?)\.?\s*(\d{1,4}[a-z]?))$",
    re.I,
)
# Where a pinpoint may begin within a citation: "NDPA s. 23(1)(a)", "ISA 2025, First Schedule, para. 3".
_STARTS = re.compile(
    rf"(?:^|(?<=[\s,;(]))(?:{_SECTION_LEADS}|{_ARTICLE_LEADS}|(?:{'|'.join(ORDINALS)}|\d{{1,2}}(?:st|nd|rd|th))\s+schedule"
    r"|schedule|sch|part|pt|chapter|chap|ch)(?=[\s.,\d(]|$)",
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
    """The number in a node's own locator token ("art_12", "art_853-1", "sch_ii"), without the suffix a
    repeated one is published with ("art_12_2")."""
    own = locator.rsplit("__", 1)[-1]
    found = re.match(rf"{token}_([0-9]+(?:-[0-9]+)*|[ivxlc]+)", own)
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
    no pinpoint names (the document, a contents list, a judgment's opinion or orders). A Part or
    Chapter of an Act is "Part V" or "Chapter 3"; what is inside it is cited by its own number."""
    key: list[str] = []
    shown = ""
    schedule = False
    opinion: str | None = None  # within a judgment's opinion: its number, "1" for the first
    lead = "reg. " if document_type == "regulation" else "s. "
    named = False  # whether the last part itself is named: a table in s. 23 is not s. 23
    for part in chain:
        before, named = (tuple(key), shown), True
        if part.kind == "opinion":
            opinion, key, shown = _token_number(part.locator, "op") or "1", [], ""
        elif opinion is not None:  # a judgment's paragraph; nothing within it has a pinpoint of its own
            number = re.sub(r"\s+", "", _printed(part.label).strip("[]"))
            if key or part.kind != "paragraph" or not number:
                return None
            key = [*([] if opinion == "1" else [f"op:{opinion}"]), f"para:{number.casefold()}"]
            shown = f"[{number}]" if opinion == "1" else f"op. {opinion} [{number}]"
        elif part.kind == "schedule":
            schedule, key = True, [f"sch:{_ordinal(_token_number(part.locator, 'sch')) or '1'}"]
            shown = _schedule_title(part.label)
        elif schedule and part.kind == "part" and part.label:
            number = re.sub(r"^part\s+", "", _printed(part.label), flags=re.I)
            key.append(f"part:{_ordinal(number)}")
            shown += f", Part {number.upper() if _ROMAN.fullmatch(number.casefold()) else number}"
        elif part.kind in ("part", "chapter") and part.label and not schedule:
            # "Part V", "Chapter 3": a section or article inside it is cited by its own number.
            word = "Part" if part.kind == "part" else "Chapter"
            number = re.sub(rf"^{word}\s+", "", _printed(part.label), flags=re.I)
            key = [f"{'part' if part.kind == 'part' else 'chap'}:{_ordinal(number)}"]
            shown = f"{word} {number.upper() if _ROMAN.fullmatch(number.casefold()) else number}"
        elif part.kind == "section" and part.label and not schedule:
            key, shown = [f"s:{_plain(part.label)}"], f"{lead}{_printed(part.label)}"
        elif part.kind == "article" and part.label:
            number = _token_number(part.locator, "art")
            key, shown = [f"art:{number}"], f"art. {number}"
        elif (
            part.kind == "paragraph"
            and key[:1]
            and key[0].startswith("art:")
            and _token_number(part.locator, "al")
        ):
            alinea = _token_number(part.locator, "al")  # counted, never printed: art_10__al_2
            key.append(alinea)
            shown += f", al. {alinea}"
        elif part.kind in ("subsection", "paragraph", "subparagraph") and part.label:
            if schedule and not any(k.startswith("para:") for k in key):
                key.append(f"para:{_plain(part.label)}")
                shown += f", para. {_printed(part.label)}"
            elif key and not key[0].startswith(("part:", "chap:")):
                key.append(_plain(part.label))
                shown += f"({_printed(part.label)})"
            else:
                return None  # a numbered paragraph outside any section
        elif part.kind == "definition" and part.label and key:
            key.append(f"def:{' '.join(part.label.split()).casefold()}")
            shown += f", “{part.label}”"
        if (tuple(key), shown) == before:
            named = False
    return (tuple(key), shown) if key and named else None


def read(pinpoint: str) -> tuple[str, ...] | None:
    """The key of a cited pinpoint, or None when it is not one this module reads."""
    value = unicodedata.normalize("NFC", " ".join(pinpoint.split())).strip(" ,;")
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
        alinea = (found.group(3) or "").casefold()
        alinea = "1" if alinea == "premier" else _plain(alinea)
        return (f"art:{number.lstrip('0') or number}", *subs(found.group(2)), *([alinea] if alinea else []))
    found = CONTAINER.match(value)
    if found:
        lead = found.group(1).casefold()
        return (f"{'part' if lead in ('part', 'pt') else 'chap'}:{_ordinal(found.group(2))}",)
    found = SCHEDULE.match(value)
    if found:
        key = [f"sch:{_ordinal(found.group(1) or found.group(2)) or '1'}"]
        if found.group(3):
            key.append(f"part:{_ordinal(found.group(3))}")
        if found.group(4):
            key += [f"para:{_plain(found.group(4))}", *subs(found.group(5))]
        return tuple(key)
    return _paragraph(value)


def _paragraph(pinpoint: str, worded: bool = False) -> tuple[str, ...] | None:
    """The key of a judgment's paragraph: "[12]", "at [12]", "op. 2 [4]"; "para. 12" only when
    ``worded`` (after a neutral citation)."""
    found = PARAGRAPH.match(pinpoint)
    if not found or (found.group(3) and not worded):
        return None
    opinion, number = found.group(1), _plain(found.group(2) or found.group(3))
    within = () if opinion is None or int(opinion) == 1 else (f"op:{int(opinion)}",)
    return (*within, f"para:{number}")


def split(citation: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Each way to read a citation as a work and a pinpoint within it, longest pinpoint first:
    "NDPA s. 23(1)(a)" → ("NDPA", "s. 23(1)(a)", key); "section 23 of the NDPA", "article 10, alinéa 2,
    de l'Acte uniforme …" and "s. 23 NDPA" likewise; a judgment by its neutral citation, alone and with
    the words before it: "Okafor v State [2019] NGSC 4 at [12]" → ("Okafor v State [2019] NGSC 4",
    "[12]", key), then ("[2019] NGSC 4", "[12]", key)."""
    value = " ".join(citation.split())
    found = []
    for neutral in NEUTRAL.finditer(value):
        pin = value[neutral.end() :].strip(" ,;")
        key = _paragraph(pin, worded=True)
        if key:
            shown = re.sub(r"^at\s+", "", pin, flags=re.I)
            for work in dict.fromkeys((value[: neutral.end()].strip(" ,;"), neutral.group(0))):
                found.append((work, shown, key))
    for start in _STARTS.finditer(value):
        work, pin = value[: start.start()].strip(" ,;("), value[start.start() :]
        key = read(pin)
        if work and key:
            found.append((work, pin.strip(" ,;"), key))
    for of in re.finditer(rf"\s+{_OF}", value, re.I):
        pin, work = value[: of.start()], value[of.end() :].strip(" ,;")
        key = read(pin)
        if work and key:
            found.append((work, pin.strip(" ,;"), key))
    # The pinpoint first, then the work: "s 24(1)(a) Nigeria Data Protection Act 2023". Only the longest
    # pinpoint, the whole citation included: "article 10" is not a reading of "article 10, alinéa 2".
    words = value.split(" ")
    for n in range(len(words), 0, -1):
        pin = " ".join(words[:n]).strip(" ,;")
        work = re.sub(r"^(?:of\s+)?(?:the\s+)?", "", " ".join(words[n:]).strip(" ,;"), flags=re.I)
        key = read(pin)
        if key is not None and key[0].split(":")[0] in ("s", "art", "sch", "part", "chap"):
            if work and all(f[1] != pin for f in found):
                found.append((work, pin, key))
            break
    return found
