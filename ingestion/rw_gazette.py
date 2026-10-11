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
        r"|AMABWIRIZA)\s+N[°ºO0]\.?\s*(\S+)"
    ),
    "en": re.compile(
        r"^(ORGANIC LAW|LAW|PRESIDENTIAL ORDER|PRIME MINISTER[’']S ORDER|MINISTERIAL ORDER|REGULATIONS?"
        r"|INSTRUCTIONS?)\s+N[°ºO0]\.?\s*(\S+)"
    ),
    "fr": re.compile(
        r"^(LOI ORGANIQUE|LOI|ARR[ÊE]T[ÉE] PR[ÉE]SIDENTIEL|ARR[ÊE]T[ÉE] DU PREMIER MINISTRE"
        r"|ARR[ÊE]T[ÉE] MINIST[ÉE]RIEL|R[ÈE]GLEMENTS?|INSTRUCTIONS?)\s+N[°ºO0]\.?\s*(\S+)"
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
    for x0, x1 in spans:
        inside = [w for w in page["words"] if x0 <= (w["xmin"] + w["xmax"]) / 2 < x1]
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


def _number(word: str) -> str:
    return "1" if re.fullmatch(ONE, word, re.I) else word


def structure(lines: list[dict], language: str) -> list[dict]:
    """Nodes of one language version: the heading, the contents, the preamble, divisions, articles
    and their paragraphs (a paragraph ends where the line spacing opens up)."""
    nodes: list[dict] = []
    part = "heading"
    gaps = [b["y0"] - a["y1"] for a, b in zip(lines, lines[1:], strict=False) if a["page"] == b["page"]]
    usual = statistics.median([g for g in gaps if g > 0] or [2.0])
    previous = None
    for line in lines:
        text = line["text"].strip()
        if not text:
            continue
        if part == "heading" and CONTENTS.match(text):
            part = "contents"
            nodes.append({"kind": "heading", "level": "heading-1", "text": text, "page": line["page"]})
            previous = line
            continue
        if part in ("heading", "contents") and PREAMBLE.match(text):
            part = "body"
        if part == "contents" and (OPENING[language].match(text) or CONSTITUTION[language].match(text)):
            nodes.append({"kind": "heading", "level": "heading-1", "text": text, "page": line["page"]})
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
        if node is None:
            joins = (
                nodes
                and previous is not None
                and nodes[-1]["kind"] == "paragraph"
                and previous["page"] == line["page"]
                and line["y0"] - previous["y1"] <= usual * 1.8
            )
            if part == "heading" and nodes and nodes[-1]["kind"] == "heading":
                nodes[-1]["text"] += " " + text
            elif joins:
                nodes[-1]["text"] += " " + text
            else:
                kind = "heading" if part == "heading" else "paragraph"
                level = "heading-1" if part == "heading" else "paragraph"
                nodes.append({"kind": kind, "level": level, "text": text, "page": line["page"]})
        else:
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


def reading(
    nodes: list[dict], furniture_by_page: dict[int, list[str]], source_sha256: str, checks: dict
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
                {k: v for k, v in node.items() if k not in ("article", "division", "unusual", "flags")}
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
