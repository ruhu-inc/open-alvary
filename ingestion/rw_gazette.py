"""Rwanda's Official Gazette: an issue split into its languages, its instruments and their articles.

Since about 2008 the Gazette prints each law or order in three columns side by side, Kinyarwanda,
English and French, article against article; earlier issues print the three versions one after
another. Every page measured so far has a text layer, so the text is taken from it (positions and
all, in the sealed container) and checked, not read again by OCR.

From an issue this module gives, for each language, its lines in reading order with their page and
position; then each instrument (title, number, date and pages in each language); then each
instrument's structure (titles, chapters, sections, articles and paragraphs), checked across the
three languages; and finally a reading per instrument and language in the shape the workbench
reviews.
"""

import re
import statistics
from collections import Counter

from ingestion import gazette_layout

LANGUAGES = ("rw", "en", "fr")

# Running headers and page numbers: printed on every page, not part of any instrument.
FURNITURE = re.compile(
    r"^(Official Gazette|Igazeti ya Leta|Journal Officiel)\b.*(n[°º]|No\b).*\d{2}/\d{2}/\d{4}\s*$"
    r"|^\d{1,4}$",
    re.I,
)

# The heading that opens an instrument, as printed in capitals in each language.
OPENING = {
    "rw": re.compile(
        r"^(ITEGEKO NGENGA|ITEGEKO|ITEKA RYA PEREZIDA|ITEKA RYA MINISITIRI W[’']INTEBE|ITEKA RYA MINISITIRI"
        r"|AMABWIRIZA)\s+N\s?[°ºO0]\.?\s*(\S+)"
    ),
    "en": re.compile(
        r"^(ORGANIC LAW|LAW|PRESIDENTIAL ORDER|PRIME MINISTER[’']S ORDER|MINISTERIAL ORDER|REGULATIONS?"
        r"|INSTRUCTIONS?)\s+N\s?[°ºO0]\.?\s*(\S+)"
    ),
    "fr": re.compile(
        r"^(LOI ORGANIQUE|LOI|ARR[ÊE]T[ÉE] PR[ÉE]SIDENTIEL|ARR[ÊE]T[ÉE] DU PREMIER MINISTRE"
        r"|ARR[ÊE]T[ÉE] MINIST[ÉE]RIEL|R[ÈE]GLEMENTS?|INSTRUCTIONS?)\s+N\s?[°ºO0]\.?\s*(\S+)"
    ),
}
# A constitution is printed without a number.
CONSTITUTION = {
    "rw": re.compile(r"^ITEGEKO NSHINGA\b"),
    "en": re.compile(r"^CONSTITUTION OF THE REPUBLIC\b"),
    "fr": re.compile(r"^CONSTITUTION DE LA R[ÉE]PUBLIQUE\b"),
}
KIND = {
    "ITEGEKO NGENGA": "organic_law",
    "ORGANIC LAW": "organic_law",
    "LOI ORGANIQUE": "organic_law",
    "ITEGEKO": "law",
    "LAW": "law",
    "LOI": "law",
    "ITEKA RYA PEREZIDA": "presidential_order",
    "PRESIDENTIAL ORDER": "presidential_order",
    "ARRÊTÉ PRÉSIDENTIEL": "presidential_order",
    "ITEKA RYA MINISITIRI W’INTEBE": "prime_ministers_order",
    "PRIME MINISTER’S ORDER": "prime_ministers_order",
    "ARRÊTÉ DU PREMIER MINISTRE": "prime_ministers_order",
    "ITEKA RYA MINISITIRI": "ministerial_order",
    "MINISTERIAL ORDER": "ministerial_order",
    "ARRÊTÉ MINISTÉRIEL": "ministerial_order",
    "AMABWIRIZA": "regulations",
    "REGULATION": "regulations",
    "REGULATIONS": "regulations",
    "RÈGLEMENT": "regulations",
    "RÈGLEMENTS": "regulations",
    "INSTRUCTION": "instructions",
    "INSTRUCTIONS": "instructions",
}
DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b")
CONTENTS = re.compile(r"^(ISHAKIRO|TABLE OF CONTENTS|TABLE DES MATI[ÈE]RES)\b", re.I)
PREAMBLE = re.compile(r"^(Twebwe|We|Nous)\s*,\s", re.I)

# Structure, in each language. "mbere", "One" and "premier" are the first of each.
ONE = r"(?i:mbere|one|premi(?:er|ère)|first)"
# An article heading: the number, then a colon (or full stop), a capitalised title or nothing;
# "Article 119 of this Law" at the start of a wrapped line is a reference, not a heading. The
# Gazette's own slips are accepted and flagged for the reviewer: "Ingingoya 2", "ngingo ya 119",
# "Ingingo 152", "Article167", and "Article 20" printed in the Kinyarwanda column.
_TAIL = r"\s*(?:[:.]\s*(.*)|\s+([A-ZÀ-Ý].*)|$)"
ARTICLE = {
    "rw": re.compile(rf"^(?:[Ii]?ngingo\s*(?:ya\s*)?|INGINGO\s+YA\s+|Article\s*)({ONE}|\d+){_TAIL}"),
    "en": re.compile(rf"^(?:Article|ARTICLE)\s*({ONE}|\d+){_TAIL}"),
    "fr": re.compile(rf"^(?:Article|ARTICLE)\s*({ONE}|\d+){_TAIL}"),
}
STANDARD_ARTICLE = {
    "rw": re.compile(rf"^Ingingo ya ({ONE}|\d+)\s?:"),
    "en": re.compile(rf"^Article ({ONE}|\d+)\s?:"),
    "fr": re.compile(rf"^Article ({ONE}|\d+)\s?:"),
}
LANGUAGE_NAMES = {"rw": "Kinyarwanda", "en": "English", "fr": "French"}
DIVISIONS = {
    "rw": [
        ("title", re.compile(r"^INTERURO\s+YA\s+(\S+)\s*[:.]?\s*(.*)$")),
        ("chapter", re.compile(r"^UMUTWE\s+WA\s+(\S+)\s*[:.]?\s*(.*)$")),
        ("section", re.compile(r"^Icyiciro\s+cya\s+(\S+)\s*[:.]?\s*(.*)$", re.I)),
        ("subsection", re.compile(r"^Akiciro\s+ka\s+(\S+)\s*[:.]?\s*(.*)$", re.I)),
    ],
    "en": [
        ("title", re.compile(r"^TITLE\s+(\S+)\s*[:.]?\s*(.*)$")),
        ("chapter", re.compile(r"^CHAPTER\s+(\S+)\s*[:.]?\s*(.*)$")),
        ("section", re.compile(r"^Section\s+(\S+)\s*[:.]?\s*(.*)$", re.I)),
        ("subsection", re.compile(r"^Sub-?section\s+(\S+)\s*[:.]?\s*(.*)$", re.I)),
    ],
    "fr": [
        ("title", re.compile(r"^TITRE\s+(\S+)\s*[:.]?\s*(.*)$")),
        ("chapter", re.compile(r"^CHAPITRE\s+(\S+)\s*[:.]?\s*(.*)$")),
        ("section", re.compile(r"^Section\s+(\S+)\s*[:.]?\s*(.*)$", re.I)),
        ("subsection", re.compile(r"^Sous-section\s+(\S+)\s*[:.]?\s*(.*)$", re.I)),
    ],
}


# --- lines and columns ------------------------------------------------------------------------------


def lines_of(words: list[dict]) -> list[dict]:
    """Words grouped into printed lines by their vertical centre, each line read left to right.
    (Underlined or bold labels sit a little off the baseline; grouping by top edge splits them.)"""
    if not words:
        return []
    height = statistics.median(w["ymax"] - w["ymin"] for w in words) or 1.0
    ordered = sorted(words, key=lambda w: (w["ymin"] + w["ymax"]) / 2)
    lines: list[list[dict]] = []
    for word in ordered:
        centre = (word["ymin"] + word["ymax"]) / 2
        if lines:
            last = lines[-1]
            last_centre = statistics.mean((w["ymin"] + w["ymax"]) / 2 for w in last)
            if abs(centre - last_centre) <= 0.45 * height:
                last.append(word)
                continue
        lines.append([word])
    out = []
    for line in lines:
        line.sort(key=lambda w: w["xmin"])
        out.append(
            {
                "text": " ".join(w["text"] for w in line),
                "x0": round(min(w["xmin"] for w in line), 1),
                "y0": round(min(w["ymin"] for w in line), 1),
                "x1": round(max(w["xmax"] for w in line), 1),
                "y1": round(max(w["ymax"] for w in line), 1),
            }
        )
    return out


def split_page(page: dict, number: int, carried: str | None = None) -> dict:
    """A page's columns, each with its language and lines; running headers set apart as furniture."""
    spans = gazette_layout.columns(page)
    furniture, columns = [], []
    three = len(spans) == 3
    # Each word to the column it lies in, or the nearest: a word alone at a column's edge (an
    # "Article" set a little apart) can fall in what the measure took for the gutter.
    by_column: list[list[dict]] = [[] for _ in spans]
    for word in page["words"]:
        centre = (word["xmin"] + word["xmax"]) / 2
        if spans:
            distance = [0 if a <= centre < b else min(abs(centre - a), abs(centre - b)) for a, b in spans]
            by_column[distance.index(min(distance))].append(word)
    for (x0, x1), inside in zip(spans, by_column, strict=True):
        kept = []
        for line in lines_of(inside):
            edge = line["y0"] < 0.09 * page["height"] or line["y1"] > 0.93 * page["height"]
            if edge and FURNITURE.match(line["text"].strip()):
                furniture.append(line)
            else:
                kept.append(line)
        language, _ = gazette_layout.language([line["text"] for line in kept])
        columns.append(
            {"language": language or carried, "x0": round(x0, 1), "x1": round(x1, 1), "lines": kept}
        )
    # Every three-column page measured prints Kinyarwanda, English and French in that order; three
    # columns whose words say otherwise (a table in one language) are not taken for the three.
    detected = [c["language"] for c in columns]
    parallel = three and all(d in (None, expected) for d, expected in zip(detected, LANGUAGES, strict=True))
    parallel = parallel and any(d == expected for d, expected in zip(detected, LANGUAGES, strict=True))
    for column, expected in zip(columns, LANGUAGES, strict=False):
        if parallel:
            column["language"] = expected
    return {
        "page": number,
        "width": page["width"],
        "height": page["height"],
        "layout": "parallel" if parallel else ("single" if len(spans) == 1 else f"{len(spans)}_columns"),
        "columns": columns,
        "furniture": furniture,
    }


def split_issue(pages: list[dict]) -> list[dict]:
    out, carried = [], None
    for number, page in enumerate(pages, 1):
        split = split_page(page, number, carried)
        languages = [c["language"] for c in split["columns"] if c["language"]]
        carried = languages[-1] if languages else carried
        out.append(split)
    return out


def streams(split: list[dict]) -> dict[str, list[dict]]:
    """Each language's lines in reading order, each tagged with its page and column."""
    out: dict[str, list[dict]] = {language: [] for language in LANGUAGES}
    for page in split:
        for column in page["columns"]:
            if column["language"] in out:
                for line in column["lines"]:
                    out[column["language"]].append(
                        {**line, "page": page["page"], "column": (column["x0"], column["x1"])}
                    )
    return out


# --- instruments --------------------------------------------------------------------------------------


def _plain_date(text: str) -> str | None:
    match = DATE.search(text)
    if not match:
        return None
    return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}"


def openings(lines: list[dict], language: str) -> list[dict]:
    """Where each instrument begins in one language: its heading, kind, number, date and title."""
    found = []
    i = 0
    while i < len(lines):
        text = lines[i]["text"].strip()
        match = OPENING[language].match(text)
        constitution = CONSTITUTION[language].match(text)
        if not (match or constitution):
            i += 1
            continue
        heading = [lines[i]["text"].strip()]
        j = i + 1
        # The title runs on in capitals until the contents or the first non-capital line.
        while j < len(lines) and len(heading) < 8:
            text = lines[j]["text"].strip()
            letters = [c for c in text if c.isalpha()]
            if CONTENTS.match(text) or not letters or sum(c.isupper() for c in letters) < 0.8 * len(letters):
                break
            heading.append(text)
            j += 1
        title = " ".join(heading)
        word = match.group(1).replace("'", "’").upper() if match else ""
        word = (
            word.replace("ARRETE", "ARRÊTÉ")
            .replace("PRESIDENTIEL", "PRÉSIDENTIEL")
            .replace("MINISTERIEL", "MINISTÉRIEL")
            .replace("REGLEMENT", "RÈGLEMENT")
        )
        found.append(
            {
                "start": i,
                "kind": KIND.get(word, "other") if match else "constitution",
                "number": match.group(2).rstrip(".,;:") if match else "constitution",
                "date": _plain_date(title),
                "title": title,
                "page": lines[i]["page"],
            }
        )
        i = j
    return found


def instruments(issue_streams: dict[str, list[dict]]) -> list[dict]:
    """The instruments of an issue, each with its title and line range in every language it is
    printed in, matched across languages by number."""
    by_number: dict[str, dict] = {}
    order: list[str] = []
    for language in LANGUAGES:
        lines = issue_streams[language]
        found = openings(lines, language)
        for k, opening in enumerate(found):
            end = found[k + 1]["start"] if k + 1 < len(found) else len(lines)
            key = opening["number"]
            if key not in by_number:
                by_number[key] = {
                    "number": key,
                    "kind": opening["kind"],
                    "date": opening["date"],
                    "versions": {},
                }
                order.append(key)
            entry = by_number[key]
            if entry["kind"] == "other":
                entry["kind"] = opening["kind"]
            entry["date"] = entry["date"] or opening["date"]
            if language in entry["versions"]:
                # The title printed again (over the body, after the contents): the same instrument.
                entry["versions"][language]["end"] = end
                entry["versions"][language]["pages"][1] = lines[end - 1]["page"]
                continue
            entry["versions"][language] = {
                "title": opening["title"],
                "start": opening["start"],
                "end": end,
                "pages": [lines[opening["start"]]["page"], lines[end - 1]["page"]],
            }
    return [by_number[key] for key in order]


# --- structure ----------------------------------------------------------------------------------------


def _capitals(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) >= 0.8 * len(letters)


def _number(word: str) -> str:
    return "1" if re.fullmatch(ONE, word, re.I) else word


def structure(lines: list[dict], language: str) -> list[dict]:
    """Nodes of one language version: the heading, the contents, the preamble, divisions, articles
    and their paragraphs (a paragraph ends where the line spacing opens up)."""
    nodes: list[dict] = []
    part = "heading"
    previous = None
    for line in lines:
        text = line["text"].strip()
        if not text:
            continue
        if part == "heading" and CONTENTS.match(text):
            part = "contents"
            nodes.append(
                {"kind": "heading", "level": "heading-1", "text": text, "page": line["page"], "lines": [line]}
            )
            previous = line
            continue
        if part in ("heading", "contents") and PREAMBLE.match(text):
            part = "body"
        if part == "contents" and (OPENING[language].match(text) or CONSTITUTION[language].match(text)):
            # The title printed again after the contents opens the body (orders have no "We, …").
            nodes.append(
                {"kind": "heading", "level": "heading-1", "text": text, "page": line["page"], "lines": [line]}
            )
            part = "body"
            previous = line
            continue
        node = None
        if part in ("body", "contents"):
            article = ARTICLE[language].match(text)
            if article:
                title = (article.group(2) or article.group(3) or "").strip()
                node = {
                    "kind": "heading" if part == "body" else "list_item",
                    "level": "heading-3" if part == "body" else "list_item",
                    "text": title,
                    "marker": text[: len(text) - len(title)].strip() if title else text,
                    "article": _number(article.group(1)),
                    "page": line["page"],
                }
                if part == "body" and not STANDARD_ARTICLE[language].match(text):
                    node["unusual"] = True
            else:
                for division, pattern in DIVISIONS[language]:
                    match = pattern.match(text)
                    if match:
                        node = {
                            "kind": "heading" if part == "body" else "list_item",
                            "level": "heading-2" if part == "body" else "list_item",
                            "text": text,
                            "division": division,
                            "page": line["page"],
                        }
                        break
        # Lines of one paragraph sit 0 to 3 pt apart, paragraphs 14 pt or more: a gap under
        # seven tenths of the line's own height continues the paragraph.
        close = (
            previous is not None
            and previous["page"] == line["page"]
            and line["y0"] - previous["y1"] < 0.7 * (line["y1"] - line["y0"])
        )
        if node is None:
            last = nodes[-1] if nodes else None
            # A heading's title, a division's name or a contents entry that wraps onto the next line.
            wraps = (
                last is not None
                and close
                and last["kind"] in ("heading", "list_item")
                and last["lines"][-1] is previous
                and (
                    last.get("division")
                    or (last.get("article") and last["text"])
                    or last["kind"] == "list_item"
                    or (last["level"] == "heading-1" and _capitals(text))
                )
            )
            if part == "heading" and last is not None and last["kind"] == "heading":
                last["text"] += " " + text
                last["lines"].append(line)
            elif wraps or (last is not None and last["kind"] == "paragraph" and close):
                last["text"] += " " + text
                last["lines"].append(line)
            else:
                kind = "heading" if part == "heading" else "paragraph"
                level = "heading-1" if part == "heading" else "paragraph"
                nodes.append(
                    {"kind": kind, "level": level, "text": text, "page": line["page"], "lines": [line]}
                )
        else:
            node["lines"] = [line]
            nodes.append(node)
        previous = line
    for node in nodes:
        node["text"] = re.sub(r"\s+", " ", node["text"]).strip()
    return nodes


def article_numbers(nodes: list[dict]) -> list[str]:
    return [n["article"] for n in nodes if n.get("article") and n["kind"] == "heading"]


def alignment(versions: dict[str, list[dict]]) -> dict:
    """Whether the language versions carry the same articles in the same order; where they do not,
    which articles are missing from or extra in each language, against the Kinyarwanda."""
    numbers = {language: article_numbers(nodes) for language, nodes in versions.items()}
    reference = numbers.get("rw") or next(iter(numbers.values()), [])
    issues = {}
    for language, found in numbers.items():
        if found != reference:
            issues[language] = {
                "missing": [n for n in reference if n not in found],
                "extra": [n for n in found if n not in reference],
                "count": len(found),
            }
    return {"articles": {k: len(v) for k, v in numbers.items()}, "aligned": not issues, "differences": issues}


# --- the reading the workbench reviews --------------------------------------------------------------------


def flag_differences(versions: dict[str, list[dict]]) -> None:
    """Mark, on each language version's nodes, what a reviewer must look at: an article the other
    languages have and this one lacks (or has twice), and a heading printed in an unusual form. The
    other languages' headings are given beside it for comparison, never as a replacement."""
    numbers = {language: article_numbers(nodes) for language, nodes in versions.items()}
    every: list[str] = []
    for found in numbers.values():
        for n in found:
            if n not in every:
                every.append(n)
    headings = {
        language: {n["article"]: n for n in nodes if n.get("article") and n["kind"] == "heading"}
        for language, nodes in versions.items()
    }
    for language, nodes in versions.items():
        body = [n for n in nodes if n.get("article") and n["kind"] == "heading"]
        seen: set[str] = set()
        for node in body:
            if node["article"] in seen:
                node.setdefault("flags", []).append(
                    {
                        "severity": "high",
                        "kind": "not_aligned",
                        "primary": node["marker"],
                        "note": "printed twice",
                    }
                )
            seen.add(node["article"])
            if node.get("unusual"):
                node.setdefault("flags", []).append(
                    {"severity": "low", "kind": "heading_form", "primary": node["marker"]}
                )
            # A title that runs on may have taken in the first line of the article where the
            # Gazette left no space after the heading.
            if len(node["text"]) > 150 or re.search(r"[.;:]\s+[A-ZÀ-Ý]|[.;]$", node["text"]):
                node.setdefault("flags", []).append(
                    {"severity": "low", "kind": "heading_long", "primary": node["text"][:80]}
                )
        for k, number in enumerate(every):
            if number in numbers[language]:
                continue
            following = next(
                (headings[language][n] for n in every[k + 1 :] if n in headings[language]),
                body[-1] if body else None,
            )
            if following is None:
                continue
            others = {
                LANGUAGE_NAMES[other]: f"{h[number]['marker']} {h[number]['text']}".strip()
                for other, h in headings.items()
                if other != language and number in h
            }
            following.setdefault("flags", []).append(
                {
                    "severity": "high",
                    "kind": "not_aligned",
                    "primary": "",
                    "missing": number,
                    "others": others,
                }
            )


def _tokens(text: str | None) -> int:
    return len((text or "").split())


def _box(lines: list[dict], size: tuple[float, float]) -> list[float]:
    width, height = size
    return [
        round(min(line["x0"] for line in lines) / width, 4),
        round(min(line["y0"] for line in lines) / height, 4),
        round(max(line["x1"] for line in lines) / width, 4),
        round(max(line["y1"] for line in lines) / height, 4),
    ]


def reading(
    nodes: list[dict],
    furniture_by_page: dict[int, list[str]],
    source_sha256: str,
    checks: dict,
    sizes: dict[int, tuple[float, float]] | None = None,
) -> dict:
    """A reading in the shape the workbench reviews: the gazette's own page numbers, each page's
    nodes in order, its running header kept as furniture, and the flags placed by word position
    (each unit's number, then its text, as the editor counts them)."""
    pages: dict[int, list[dict]] = {}
    for node in nodes:
        pages.setdefault(node["page"], []).append(node)
    out = []
    for number in sorted(pages):
        flags, position, page_nodes = [], 0, []
        for node in pages[number]:
            marker, words = _tokens(node.get("marker")), _tokens(node["text"])
            for flag in node.get("flags", []):
                if flag["kind"] == "not_aligned" and flag.get("missing"):
                    span = [position, position]
                else:
                    span = [position, position + max(marker, 1)]
                flags.append(
                    {
                        "severity": flag["severity"],
                        "kind": flag["kind"],
                        "primary": flag["primary"],
                        "checks": flag.get("others", {}),
                        "compare": True,
                        "tokens": span,
                        **({"missing": flag["missing"]} if flag.get("missing") else {}),
                    }
                )
            position += marker + words
            page_nodes.append(
                {
                    k: v
                    for k, v in node.items()
                    if k not in ("article", "division", "unusual", "flags", "lines")
                }
            )
        page_nodes = [
            {"kind": "furniture", "text": text, "page": number} for text in furniture_by_page.get(number, [])
        ] + page_nodes
        out.append(
            {
                "page": number,
                "nodes": page_nodes,
                "text": "\n".join(n["text"] for n in page_nodes if n["kind"] != "furniture"),
                "tables": [],
                "furniture": furniture_by_page.get(number, []),
                "flags": flags,
                "checks": {},
                # Where each unit and each printed line sits on the page image (fractions of the
                # page), so the reviewer sees this language's column, from the text layer itself.
                **(
                    {
                        "regions": [
                            {
                                "text": " ".join(filter(None, [node.get("marker"), node["text"]])),
                                "role": None,
                                "box": _box(
                                    [ln for ln in node["lines"] if ln["page"] == number], sizes[number]
                                ),
                            }
                            for node in pages[number]
                            if node.get("lines")
                        ],
                        "lines": [
                            {"text": ln["text"], "box": _box([ln], sizes[number])}
                            for node in pages[number]
                            for ln in node.get("lines", [])
                            if ln["page"] == number
                        ],
                    }
                    if sizes and number in sizes
                    else {}
                ),
            }
        )
    counts = Counter(f["severity"] for page in out for f in page["flags"])
    return {
        "input_sha256": source_sha256,
        "primary": {"reader": "text-layer", "version": {"tool": "pdftotext -bbox-layout (poppler)"}},
        "checks": checks,
        "summary": {
            "pages": len(out),
            "flags": {level: counts.get(level, 0) for level in ("high", "medium", "low")},
            "restored_markers": 0,
            "missing_checks": [],
        },
        "pages": out,
    }


def kinds_count(found: list[dict]) -> dict:
    return dict(Counter(item["kind"] for item in found))


# How each kind of instrument is named in a citation, in each language.
CITED_AS = (
    "Organic Law",
    "Law",
    "Presidential Order",
    "Prime Minister's Order",
    "Ministerial Order",
    "Itegeko Ngenga",
    "Itegeko",
    "Iteka rya Perezida",
    "Iteka rya Minisitiri w’Intebe",
    "Iteka rya Minisitiri",
    "Loi organique",
    "Loi",
    "Arrêté présidentiel",
    "Arrêté du Premier Ministre",
    "Arrêté ministériel",
)


def sentence_case(title: str) -> str:
    """A capitalised printed title as a citation is written: "Organic Law n° 002/2018.OL of
    04/04/2018 establishing the Court of Appeal"; the number keeps its printed capitals."""
    if not title:
        return title
    number = re.search(r"\bN\s?[°ºO]\.?\s*(\S+)", title)
    sentence = title[0] + title[1:].lower()
    for name in CITED_AS:
        if sentence.lower().startswith(name.lower() + " "):
            sentence = name + sentence[len(name) :]
            break
    sentence = re.sub(
        r"\bn\s?[°ºo]\.?\s*(\S+)",
        lambda m: "n° " + (number.group(1) if number else m.group(1)),
        sentence,
        count=1,
    )
    return re.sub(r"\b(rwanda|republic|république|repubulika)\b", lambda m: m.group(1).capitalize(), sentence)


# --- details read from the text ------------------------------------------------------------------------

MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "janvier", "mutarama"),
            ("february", "février", "gashyantare"),
            ("march", "mars", "werurwe"),
            ("april", "avril", "mata"),
            ("may", "mai", "gicurasi"),
            ("june", "juin", "kamena"),
            ("july", "juillet", "nyakanga"),
            ("august", "août", "kanama"),
            ("september", "septembre", "nzeri"),
            ("october", "octobre", "ukwakira"),
            ("november", "novembre", "ugushyingo"),
            ("december", "décembre", "ukuboza"),
        ],
        1,
    )
    for name in names
}
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
SPOKEN_DATE = re.compile(
    rf"(?:(\d{{1,2}})\s*(?:st|nd|rd|th)?\s+({_MONTH})\s*,?\s*((?:19|20)\d\d)"
    rf"|({_MONTH})\s+(\d{{1,2}})\s*(?:st|nd|rd|th)?\s*,?\s*((?:19|20)\d\d))",
    re.I,
)
SITTING = {
    "deputies": re.compile(
        r"Chamber of Deputies,?\s+in its (?:plenary )?(?:session|sitting) of\s+(.{6,40}?\d{4})", re.I
    ),
    "senate": re.compile(r"\bSenate,?\s+in its (?:plenary )?(?:session|sitting) of\s+(.{6,40}?\d{4})", re.I),
}
KIGALI = re.compile(r"Kigali\s*,\s*(?:on\s+)?(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*((?:19|20)\d\d)", re.I)
IN_FORCE_ON_PUBLICATION = re.compile(
    r"(?:comes|shall come|enters) into force on the date of its publication", re.I
)
# "Law n° 31/2009 of 26/10/2009 on the protection of intellectual property as amended to date is
# repealed": the law it repeals, by its citation as printed.
REPEALS = re.compile(
    r"((?:Organic\s+)?Law\s+n\s?[°ºo]\s*[\w/.]+\s+of\s+\d{1,2}/\d{1,2}/\d{4}[^.;]{0,200}?)"
    r"\s+(?:as\s+amended\s+to\s+date\s+)?(?:is|are)\s+(?:hereby\s+)?repealed",
    re.I,
)
ADOPTED_IN = re.compile(r"adopted in (Ikinyarwanda|Kinyarwanda|English|French)", re.I)
ROLES = (
    ("promulgated", re.compile(r"^(?:The\s+)?President of the Republic$", re.I)),
    ("countersigned", re.compile(r"^(?:The\s+)?Prime Minister$", re.I)),
    ("sealed", re.compile(r"^(?:The\s+)?Minister of Justice\b.*$", re.I)),
)
ANY_ROLE = re.compile(r"^(?:The\s+)?(President of the Republic|Prime Minister|Minister\b.*)$", re.I)
SIGNED = re.compile(r"^\(s[ée]\)$", re.I)
NAME = re.compile(r"\b[A-ZÀ-Ý][A-ZÀ-Ý’'-]{2,}\b(?!\s+(?:OF|DE|YA)\b)")  # a surname in capitals


def spoken_date(text: str) -> str | None:
    """'04 December 2023', '5 June 2012', 'March 4 th , 2005' -> ISO date."""
    match = SPOKEN_DATE.search(text)
    if not match:
        return None
    if match.group(1):
        day, month, year = match.group(1), match.group(2), match.group(3)
    else:
        day, month, year = match.group(5), match.group(4), match.group(6)
    return f"{year}-{MONTHS[month.lower()]:02d}-{int(day):02d}"


def signatories(lines: list[str]) -> dict[str, str]:
    """Who signed, from the block after "Kigali, on …": the President, the Prime Minister and the
    Minister of Justice who seals. Older laws print the office before the name, newer ones after."""
    start = next((i for i, text in enumerate(lines) if KIGALI.search(text)), None)
    if start is None:
        return {}
    block = [
        t.strip()
        for t in lines[start + 1 :]
        if t.strip()
        and not SIGNED.match(t.strip())
        and not FURNITURE.match(t.strip())
        and not re.match(r"^(Seen and sealed|Bibonywe kandi|Vu et scell)|^Republic:$", t.strip(), re.I)
    ]
    first_role = next((i for i, t in enumerate(block) if ANY_ROLE.match(t)), None)
    if first_role is None:
        return {}
    name_first = any(NAME.search(t) for t in block[:first_role])
    found: dict[str, str] = {}
    for i, text in enumerate(block):
        if not ANY_ROLE.match(text):
            continue
        # The nearest name, passing over the office printed in the other two languages.
        step_by = -1 if name_first else 1
        j = i + step_by
        while 0 <= j < len(block) and not NAME.search(block[j]) and abs(j - i) <= 3:
            j += step_by
        if not 0 <= j < len(block) or not NAME.search(block[j]) or ANY_ROLE.match(block[j]):
            continue
        role = re.sub(r"^The\s+", "", text, flags=re.I)
        for step, pattern in ROLES:
            if pattern.match(text) and step not in found:
                found[step] = f"{block[j]}, {role}"
    return found


def suggest(versions: dict[str, list[dict]], entry: dict, issue: dict) -> dict:
    """Details of the record read from the instrument as printed: its title, number and dates, the
    sittings that adopted it, who promulgated, countersigned and sealed it and when, where it was
    published, when it came into force and the language it was adopted in. Values only; the
    reviewer confirms each against the page."""
    english = [line["text"] for line in versions.get("en", [])]
    text = " ".join(english)
    every = [line["text"] for lines in versions.values() for line in lines]
    values: dict = {"identity.type": entry["kind"]}
    if entry["kind"] == "constitution":
        values["identity.family"] = "constitution"
    elif entry["kind"] in ("law", "organic_law", "decree_law"):
        values["identity.family"] = "legislation"
    else:
        values["identity.family"] = "executive_instrument"
    title = entry["versions"].get("en", next(iter(entry["versions"].values())))["title"]
    if title:
        values["identity.full_title"] = values["identity.title"] = sentence_case(title)
        values["identity.citation"] = values["identity.full_title"]
    if entry["number"] != "constitution":
        values["identity.number.value"] = entry["number"]
        printed = {
            "law": "Law",
            "organic_law": "Organic Law",
            "presidential_order": "Presidential Order",
            "prime_ministers_order": "Prime Minister's Order",
            "ministerial_order": "Ministerial Order",
        }
        if entry["kind"] in printed:
            values["identity.number.printed"] = f"{printed[entry['kind']]} n° {entry['number']}"
    if entry.get("date"):
        values["identity.year"] = entry["date"][:4]
    if entry["kind"] in ("law", "organic_law", "constitution"):
        values["issued_by"] = "Parliament of the Republic of Rwanda"
    for step, pattern in SITTING.items():
        match = pattern.search(text)
        if match and spoken_date(match.group(1)):
            values[f"step:adopted:{step}.date"] = spoken_date(match.group(1))
    kigali = next((KIGALI.search(t) for t in every if KIGALI.search(t)), None)
    if kigali:
        when = f"{kigali.group(3)}-{int(kigali.group(2)):02d}-{int(kigali.group(1)):02d}"
        for step in ("promulgated", "countersigned", "sealed"):
            values[f"step:{step}.date"] = when
    for language in ("en", *[k for k in versions if k != "en"]):
        people = signatories([line["text"] for line in versions.get(language, [])])
        for step, person in people.items():
            values.setdefault(f"step:{step}.person", person)
    if issue.get("date"):
        values["step:published.date"] = issue["date"]
        values["publication.date"] = issue["date"]
        if IN_FORCE_ON_PUBLICATION.search(text):
            values["step:commenced.date"] = issue["date"]
            if values["identity.family"] in ("legislation", "constitution"):
                values["profile.commencement"] = "on_publication"
    repealed = [
        {"kind": "repeals", "target": re.sub(r"\s+", " ", match.group(1)).strip()}
        for match in REPEALS.finditer(text)
    ]
    if repealed:
        values["relations"] = repealed
    values["publication.kind"] = "gazette"
    values["publication.name"] = "Official Gazette of the Republic of Rwanda"
    edition = "Special" if issue.get("special") else str(issue.get("number") or "")
    edition += " Bis" if issue.get("bis") else (" Ter" if issue.get("ter") else "")
    if edition.strip():
        values["publication.number"] = edition.strip()
    values["publication.publisher"] = "Ministry of Justice"
    values["text.form"] = "gazette_publication"
    adopted = ADOPTED_IN.search(text)
    authority = "rw" if adopted and adopted.group(1).lower() in ("ikinyarwanda", "kinyarwanda") else None
    if adopted and adopted.group(1).lower() == "english":
        authority = "en"
    values["expressions"] = [
        {
            "language": language,
            "script": "Latn",
            "direction": "ltr",
            **(
                {"authority": "authoritative" if language == authority else "official_translation"}
                if authority
                else {}
            ),
        }
        for language in LANGUAGES
        if language in entry["versions"]
    ]
    purpose = next(
        (
            n
            for n in versions_nodes(versions.get("en", []))
            if n["kind"] == "paragraph" and n.get("after_article") == "1" and n.get("purpose")
        ),
        None,
    )
    if purpose:
        values["text.summary.text"] = purpose["text"]
    return values


def versions_nodes(lines: list[dict]) -> list[dict]:
    """The English body's paragraphs, each with the article it follows."""
    nodes = structure(lines, "en") if lines else []
    current, purpose = None, False
    for node in nodes:
        if node.get("article") and node["kind"] == "heading":
            current = node["article"]
            purpose = bool(re.search(r"purpose|object|scope|aim", node["text"], re.I))
        elif node["kind"] == "paragraph":
            node["after_article"], node["purpose"] = current, purpose
    return nodes
