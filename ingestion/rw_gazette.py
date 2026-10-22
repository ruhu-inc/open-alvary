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
import unicodedata
from bisect import bisect_right
from collections import Counter
from functools import cache
from itertools import accumulate, pairwise

from ingestion import gazette_layout
from schema.jurisdictions import registry

# The version of what this module makes of an issue: raised whenever its output changes, so that
# issues prepared by an older version are prepared again (BucketCollection.done).
PIPELINE = "2026-10-22.2"  # a turned (landscape) page measured as it is shown (gazette_layout.words)
LANGUAGES = ("rw", "en", "fr")

# Running headers and page numbers: printed on every page, not part of any instrument.
# A running header: the Gazette's name (in full, or J.O., O.G., B.O. on older issues), its number
# and date, perhaps with the page number before or after it; or a page number alone ("52", "- 52 -").
PAGE_NUMBER = r"[-–—]\s*\d{1,4}\s*[-–—]"
FURNITURE = re.compile(
    rf"^(?:{PAGE_NUMBER}\s*)?"
    r"(Official Gazette|Igazeti ya Leta|Journal Officiel|Bulletin Officiel|J\.\s?O\.?|O\.\s?G\.?|B\.\s?O\.?)"
    r"\s*.*(n[°ºᵒ*]|No\b|sp[ée]cial).*"
    r"(\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{1,2}(?:st|nd|rd|th|er)?\s+[A-Za-zÀ-ÿ]+\s+\d{4})\.?"
    rf"\s*(?:{PAGE_NUMBER})?\s*$"
    rf"|^\d{{1,4}}$|^{PAGE_NUMBER}$",
    re.I,
)

# The heading that opens an instrument, as printed in capitals in each language: its kind, then its
# number ("N° 11/2001"), or on older laws only its date ("LOI DU 19 FÉVRIER 1964", "ITEGEKO RYO KUWA
# 19 GASHYANTARE 1964"). Older kinds: decree-laws, ordinances, decrees, royal orders. A number
# printed "N° 001 BIS /2021" is the whole of it, not "001" (which is another law).
_NUMBER = r"\s+N\s?[°ºO0]\.?\s*(\S+?(?:\s*(?i:BIS|TER)\b)?(?:\s*/\s*\S+)?)(?=\s|$)"
OPENING = {
    "rw": re.compile(
        r"^(ITEGEKO NGENGA|ITEGEKO-TEKA|ITEGEKO|ITEKA RYA PEREZIDA|ITEKA RYA MINISITIRI W[’']INTEBE"
        r"|ITEKA RYA MINISITIRI|ITEKA RYA MINISTRI|AMABWIRIZA)"
        rf"(?:{_NUMBER}|\s+R?YO\s+KU\s?WA\s+(.*\d{{4}}))"
    ),
    "en": re.compile(
        r"^(ORGANIC LAW|DECREE-LAW|LAW|PRESIDENTIAL ORDER|PRIME MINISTER[’']S ORDER|MINISTERIAL ORDER"
        r"|REGULATIONS?|INSTRUCTIONS?|DECREE|ORDINANCE)"
        rf"(?:{_NUMBER}|\s+OF\s+(.*\d{{4}}))"
    ),
    "fr": re.compile(
        r"^(LOI ORGANIQUE|D[ÉE]CRET-LOI|ORDONNANCE-LOI|ORDONNANCE L[ÉE]GISLATIVE|LOI|ARR[ÊE]T[ÉE] PR[ÉE]SIDENTIEL"
        r"|ARR[ÊE]T[ÉE] DU PREMIER MINISTRE|ARR[ÊE]T[ÉE] MINIST[ÉE]RIEL|ARR[ÊE]T[ÉE] ROYAL|R[ÈE]GLEMENTS?"
        r"|INSTRUCTIONS?|ORDONNANCE|D[ÉE]CRET)"
        rf"(?:{_NUMBER}|\s+DU\s+(.*\d{{4}}))"
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
    # Older kinds (before 1994 and in RLRC's repealed laws).
    "ITEGEKO-TEKA": "decree_law",
    "DECREE-LAW": "decree_law",
    "DÉCRET-LOI": "decree_law",
    "ORDONNANCE-LOI": "ordinance",
    "ORDONNANCE LÉGISLATIVE": "ordinance",
    "ORDONNANCE": "ordinance",
    "ORDINANCE": "ordinance",
    "DÉCRET": "decree",
    "DECREE": "decree",
    "ARRÊTÉ ROYAL": "royal_decree",
    "ITEKA RYA MINISTRI": "ministerial_order",
}
# A date in figures: "12/03/1999", and on older pages "14-3-1974" or "1.3.1964".
DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-]((?:19|20)\d\d)\b")
CONTENTS = re.compile(r"^(ISHAKIRO|TABLE OF CONTENTS|TABLE DES MATI[ÈE]RES)\b", re.I)
# Where the body begins: a law's "We, KAGAME Paul," or an order's own author ("The Minister of …;",
# "Minisitiri w'…;", "Le Ministre …;"), when an order has no contents list printed again after it.
PREAMBLE = re.compile(
    r"^(Twebwe|We|Nous)\s*,\s|^(The\s+(Prime\s+)?Minister|Minisitiri|Le\s+(Premier\s+)?Ministre|La\s+Ministre)\b.{0,120};$"
    # A treaty (RLRC's international instruments) heads its preamble.
    r"|^(PR[ÉE]AMBULE|PREAMBLE|IRANGASHINGIRO)\s*:?$",
    re.I,
)
# A title never runs on to another page, nor past this length (the longest printed is about 300
# characters): beyond either, the body has begun even where nothing above recognised its start.
HEADING_MAX = 600

# Structure, in each language. "mbere", "One" and "premier" are the first of each.
ONE = r"(?i:mbere|one|premi(?:er|ère)|first)"
# Treaties number their articles in Roman numerals: "Article I", "Article XIV" (never empty).
ROMAN = r"(?=[IVXL])(?:XL|L?X{0,3})(?:IX|IV|V?I{0,3})(?![A-Za-z])"
# An article heading: the number, then a colon (or full stop), a capitalised title or nothing;
# "Article 119 of this Law" at the start of a wrapped line is a reference, not a heading. The
# Gazette's own slips are accepted and flagged for the reviewer: "Ingingoya 2", "ngingo ya 119",
# "Ingingo 152", "Article167", and "Article 20" printed in the Kinyarwanda column.
_TAIL = r"\s*(?:[:.]\s*(.*)|\s+([A-ZÀ-Ý].*)|$)"
ARTICLE = {
    "rw": re.compile(rf"^(?:[Ii]?ngingo\s*(?:ya\s*)?|INGINGO\s+YA\s+|Article\s*)({ONE}|\d+|{ROMAN}){_TAIL}"),
    # Older pages abbreviate it: "Art. 2.", "Art. 1er." (the 1970s Gazette).
    "en": re.compile(rf"^(?:Article|ARTICLE|Art\.)\s*({ONE}|\d+|{ROMAN}){_TAIL}"),
    "fr": re.compile(rf"^(?:Article|ARTICLE|Art\.)\s*({ONE}|\d+|{ROMAN})(?:er|ère)?{_TAIL}"),
}
STANDARD_ARTICLE = {
    "rw": re.compile(rf"^Ingingo ya ({ONE}|\d+|{ROMAN})\s?:"),
    "en": re.compile(rf"^Article ({ONE}|\d+|{ROMAN})\s?:"),
    "fr": re.compile(rf"^Article ({ONE}|\d+|{ROMAN})\s?:"),
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


# A raised "º" set as its own small word: "Loi n o 71/2024" is "Loi nº 71/2024".
RAISED_O = re.compile(r"\b([Nn]) o (?=\d)")


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
                "text": RAISED_O.sub(r"\1º ", " ".join(w["text"] for w in line)),
                "x0": round(min(w["xmin"] for w in line), 1),
                "y0": round(min(w["ymin"] for w in line), 1),
                "x1": round(max(w["xmax"] for w in line), 1),
                "y1": round(max(w["ymax"] for w in line), 1),
            }
        )
        if any("confidence" in w for w in line):
            # Read by our OCR: the words it was unsure of, which a second reading may replace.
            out[-1]["doubtful"] = [w["text"] for w in line if doubtful(w)]
    return out


# A word our OCR read with little confidence, or that no Rwandan text spells: letters of another
# script, a dotless ı, a full stop inside a lower-case word ("kwisuzum.sha" for broken type).
STRANGE = re.compile(r"[^\x00-\x7f\u00c0-\u024f\u2018-\u201f«»°º–—…§€]|ı|[a-zà-ÿ]\.[a-zà-ÿ]{2,}")
SURE = 0.9


def doubtful(word: dict) -> bool:
    return word.get("confidence", 1.0) < SURE or bool(STRANGE.search(word["text"]))


def held_gutters(words: list[dict], spans: list[tuple[float, float]]) -> bool:
    """Whether another page's columns hold on this one: words on each side of every gutter, and few
    crossing it (a centred date or signature)."""
    if len(spans) < 2 or not words:
        return False
    for (_, end), (start, _) in pairwise(spans):
        gutter = (end + start) / 2
        crossing = sum(1 for w in words if w["xmin"] < gutter < w["xmax"])
        left = sum(1 for w in words if (w["xmin"] + w["xmax"]) / 2 < gutter)
        if crossing > max(3, 0.08 * len(words)) or min(left, len(words) - left) < 3:
            return False
    return True


def lopsided(words: list[dict], spans: list[tuple[float, float]]) -> bool:
    """Whether a page's own columns leave one nearly empty (under 15% of its words; a language column
    holds nearer half): the measure put a gutter where there is none, as a signature centred across
    the real one can make it do."""
    if len(spans) < 2 or not words:
        return False
    counts = [0] * len(spans)
    for w in words:
        centre = (w["xmin"] + w["xmax"]) / 2
        counts[
            min(
                range(len(spans)),
                key=lambda i: (
                    0
                    if spans[i][0] <= centre < spans[i][1]
                    else min(abs(centre - spans[i][0]), abs(centre - spans[i][1]))
                ),
            )
        ] += 1
    return min(counts) < 0.15 * len(words)


def split_page(
    page: dict,
    number: int,
    carried: str | None = None,
    tables: list[dict] | None = None,
    spans: list[tuple[float, float]] | None = None,
) -> dict:
    """A page's columns, each with its language and lines; running headers set apart as furniture.
    `spans` gives the columns instead of measuring them (split_issue: the page before's)."""
    # Columns are measured without the tables' words: a table's amount columns are not languages.
    in_tables = {id(w) for table in tables or [] for w in table["words"]}
    if spans is None:
        spans = gazette_layout.columns(
            {**page, "words": [w for w in page["words"] if id(w) not in in_tables]}
        )
    furniture, columns = [], []
    three = len(spans) == 3
    # Each word to the column it lies in, or the nearest: a word alone at a column's edge (an
    # "Article" set a little apart) can fall in what the measure took for the gutter.
    by_column: list[list[dict]] = [[] for _ in spans]
    for word in page["words"]:
        if id(word) in in_tables:
            continue
        centre = (word["xmin"] + word["xmax"]) / 2
        if spans:
            distance = [0 if a <= centre < b else min(abs(centre - a), abs(centre - b)) for a, b in spans]
            by_column[distance.index(min(distance))].append(word)

    def nearest(centre: float) -> int:
        distance = [0 if a <= centre < b else min(abs(centre - a), abs(centre - b)) for a, b in spans]
        return distance.index(min(distance))

    home = (
        {id(table): nearest((table["box"][0] + table["box"][2]) / 2) for table in tables or []}
        if spans
        else {}
    )
    for index, ((x0, x1), inside) in enumerate(zip(spans, by_column, strict=True)):
        kept = []
        for position, line in enumerate(lines_of(inside)):
            # Running headers sit in the top tenth (or open the column, on pages with a deeper top
            # margin); page numbers lower than one might think (about 0.9).
            edge = position < 2 or line["y0"] < 0.09 * page["height"] or line["y1"] > 0.85 * page["height"]
            if edge and FURNITURE.match(line["text"].strip()):
                furniture.append(line)
            else:
                kept.append(line)
        for table in tables or []:
            tx0, ty0, tx1, ty1 = table["box"]
            if home.get(id(table)) == index:
                kept.append(
                    {
                        "text": "",
                        "table": {k: table[k] for k in ("header_rows", "body_rows")},
                        "x0": round(tx0, 1),
                        "y0": round(ty0, 1),
                        "x1": round(tx1, 1),
                        "y1": round(ty1, 1),
                    }
                )
        kept.sort(key=lambda line: line["y0"])
        # A column's language from all its words, its tables' included: a page of figures with a
        # line or two of text is still in the language of its headings and item names.
        language, _ = gazette_layout.language(
            [line["text"] for line in kept]
            + [
                cell["text"]
                for line in kept
                if line.get("table")
                for row in line["table"]["body_rows"]
                for cell in row
            ]
            + [
                cell["text"]
                for line in kept
                if line.get("table")
                for row in line["table"]["header_rows"]
                for cell in row
            ]
        )
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


def split_issue(pages: list[dict], tables: dict[int, list[dict]] | None = None) -> list[dict]:
    """Every page split; `tables` are the ruled tables found on each page (ruled_tables), whose
    words then come out as tables and not as lines of text. A page keeps its own columns unless they
    leave one nearly empty while the page before's languages side by side hold on it and read there
    as the same languages, in the same order (rw-decree-law-20-76, page 2: a signature centred across
    the gutter moved the measured gutter to the margin and read the Kinyarwanda into the French).
    Pipelines .1 to .3 took an earlier page's columns whenever they held, and lost 3,588 orders."""
    out, carried, previous = [], None, None
    for number, page in enumerate(pages, 1):
        found = (tables or {}).get(number)
        split = split_page(page, number, carried, found)
        if previous:
            in_tables = {id(w) for table in found or [] for w in table["words"]}
            words = [w for w in page["words"] if id(w) not in in_tables]
            own = [(c["x0"], c["x1"]) for c in split["columns"]]
            if len(own) == len(previous[0]) and lopsided(words, own) and held_gutters(words, previous[0]):
                kept = split_page(page, number, carried, found, spans=previous[0])
                if [c["language"] for c in kept["columns"]] == previous[1]:
                    split = kept
        languages = [c["language"] for c in split["columns"] if c["language"]]
        carried = languages[-1] if languages else carried
        # The page's columns, for the next page, when they are languages side by side: each a different
        # language, about as wide as the others (a table's columns are neither).
        sides = [c["language"] for c in split["columns"]]
        widths = [c["x1"] - c["x0"] for c in split["columns"]]
        distinct = (
            len(sides) > 1
            and None not in sides
            and len(set(sides)) == len(sides)
            and max(widths) <= 1.5 * min(widths)
        )
        previous = ([(c["x0"], c["x1"]) for c in split["columns"]], sides) if distinct else None
        out.append(split)
    return out


# An annex opens on a page headed "UMUGEREKA W'ITEKA … / ANNEX TO … / ANNEXE À …" and closes where its
# signatories attest it: "Bibonywe kugira ngo bishyirwe ku mugereka … / Seen to be annexed to … /
# Vu pour être annexé à …". Between them it is often in one language only (an agreement, a plan,
# the budget's tables), so its pages are kept out of the instrument's language versions.
ANNEX_START = re.compile(r"^(UMUGEREKA|ANNEXE?|SCHEDULE|IMIGEREKA)\b")
# The formal title of an annex names what it is annexed to: "ANNEX II TO LAW N° …", "UMUGEREKA WA II
# W'ITEGEKO …", "ANNEXE À L'ARRÊTÉ …". A page merely headed "ANNEX II-1: …" inside an annex goes on.
ANNEX_TITLE = re.compile(
    r"^(UMUGEREKA\s+(?:WA\s+\S+\s+)?W|IMIGEREKA\s+Y|ANNEXE?S?\s+(?:[IVXLC\d]+(?:-\d+)?\s+)?(?:TO|À|A|AUX?)\b)"
)
ANNEXED = re.compile(
    r"Seen to be annexed|Bibonywe kugira ngo bishyirwe ku mugereka|Vu pour [êe]tre annex", re.I
)


def _opens(page: dict, pattern: re.Pattern, lines: int = 2) -> bool:
    return any(pattern.match(line["text"].strip()) for c in page["columns"] for line in c["lines"][:lines])


def annex_pages(split: list[dict]) -> dict[int, int]:
    """Which pages belong to an annex: page number -> the annex's position in the issue (1, 2, …)."""
    roles: dict[int, int] = {}
    count, inside = 0, False
    for page in split:
        if _opens(page, ANNEX_TITLE) or (not inside and _opens(page, ANNEX_START)):
            count, inside = count + 1, True
        elif inside and any(
            OPENING[c["language"]].match(line["text"].strip())
            for c in page["columns"]
            if c["language"] in OPENING
            for line in c["lines"][:2]
        ):
            inside = False  # the next instrument begins
        if inside:
            roles[page["page"]] = count
            if any(ANNEXED.search(line["text"]) for c in page["columns"] for line in c["lines"]):
                inside = False
    return roles


def annexes(split: list[dict], roles: dict[int, int]) -> list[dict]:
    """Each annex with its pages and its lines in every language it is printed in."""
    found: dict[int, dict] = {}
    for page in split:
        number = roles.get(page["page"])
        if number is None:
            continue
        annex = found.setdefault(number, {"annex": number, "pages": [], "versions": {}})
        annex["pages"].append(page["page"])
        for column in page["columns"]:
            language = column["language"] or "en"
            annex["versions"].setdefault(language, []).extend(
                {**line, "page": page["page"], "column": (column["x0"], column["x1"])}
                for line in column["lines"]
            )
    for annex in found.values():
        first = next(iter(annex["versions"].values()), [])
        annex["title"] = " ".join(line["text"] for line in first[:6] if line["text"])[:300]
        annex["refers_to"] = next(
            (m.group(1) for line in first[:8] if (m := re.search(r"N\s?[°ºO0]\.?\s*(\S+)", line["text"]))),
            None,
        )
    return list(found.values())


def streams(split: list[dict], skip: set[int] | frozenset = frozenset()) -> dict[str, list[dict]]:
    """Each language's lines in reading order, each tagged with its page and column (pages in `skip`,
    an annex's, are left out)."""
    out: dict[str, list[dict]] = {language: [] for language in LANGUAGES}
    for page in split:
        if page["page"] in skip:
            continue
        for column in page["columns"]:
            if column["language"] in out:
                for line in column["lines"]:
                    out[column["language"]].append(
                        {**line, "page": page["page"], "column": (column["x0"], column["x1"])}
                    )
    return out


# --- instruments --------------------------------------------------------------------------------------


def _plain_date(text: str) -> str | None:
    for match in DATE.finditer(text):
        day, month = int(match.group(1)), int(match.group(2))
        if 1 <= day <= 31 and 1 <= month <= 12:  # "06-81-1981" is a number, not a date
            return f"{match.group(3)}-{month:02d}-{day:02d}"
    return None


def _instrument_number(printed: str) -> str:
    """A number as printed in a heading, in one spelling: "001 BIS /2021" -> "001bis/2021"."""
    number = re.sub(r"\s*/\s*", "/", printed).rstrip(".,;:")
    return re.sub(r"(\d)\s*(bis|ter)\b", lambda m: m.group(1) + m.group(2).lower(), number, flags=re.I)


def openings(lines: list[dict], language: str) -> list[dict]:
    """Where each instrument begins in one language: its heading, kind, number, date and title."""
    found = []
    i = 0
    while i < len(lines):
        text = lines[i]["text"].strip()
        if re.search(r"\bN\s?[°ºO0]\.?$", text) and i + 1 < len(lines):  # the number on the next line
            text = f"{text} {lines[i + 1]['text'].strip()}"
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
            .replace("DECRET", "DÉCRET")
            .replace("LEGISLATIVE", "LÉGISLATIVE")
        )
        date = _plain_date(title) or spoken_date(title)
        number = _instrument_number(match.group(2) or "") if match else "constitution"
        if not number:
            # An older law known by its date: the date is its key across the languages.
            if not date:
                i += 1
                continue
            number = date
        found.append(
            {
                "start": i,
                "kind": KIND.get(word, "other") if match else "constitution",
                "number": number,
                "date": date,
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
            opening["has_body"] = any(
                PREAMBLE.match(line["text"].strip()) for line in lines[opening["start"] : end]
            )
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
                "has_body": opening["has_body"],
                "start": opening["start"],
                "end": end,
                "pages": [lines[opening["start"]]["page"], lines[end - 1]["page"]],
            }
    found = [by_number[key] for key in order]
    # A title over the contents and the same title over the body printed with another number ("LAW N°
    # 066/2024" then "LAW N° 064/2024", same date and words) are one instrument: kept as one, both
    # numbers recorded for the reviewer to settle.
    merged: list[dict] = []
    for entry in found:
        previous = merged[-1] if merged else None
        if previous and _same_title_other_number(previous, entry):
            for language, version in entry["versions"].items():
                if language in previous["versions"]:
                    version["start"] = previous["versions"][language]["start"]
                    version["pages"][0] = previous["versions"][language]["pages"][0]
            entry["also_printed_as"] = previous["number"]
            merged[-1] = entry
        else:
            merged.append(entry)
    return merged


def whole_file(
    issue_streams: dict[str, list[dict]], kind: str, number: str, date: str | None, title: str
) -> list[dict]:
    """A file that is one instrument whose heading the openings do not know (an RLRC copy of an
    older text): one entry over each language printed in it, its title from the source's listing,
    marked so that the reviewer checks it."""
    versions = {}
    for language, lines in issue_streams.items():
        if sum(len(line["text"].split()) for line in lines) >= 20:  # the language is printed here
            versions[language] = {
                "title": title,
                "has_body": True,
                "start": 0,
                "end": len(lines),
                "pages": [lines[0]["page"], lines[-1]["page"]],
            }
    if not versions:
        return []
    return [{"number": number, "kind": kind, "date": date, "versions": versions, "title_from_listing": True}]


def _same_title_other_number(first: dict, second: dict) -> bool:
    if (
        first["kind"] != second["kind"]
        or first["date"] != second["date"]
        or first["number"] == second["number"]
    ):
        return False
    shared = set(first["versions"]) & set(second["versions"])
    if not shared:
        return False

    def words(entry, language):
        return re.sub(r"\bN\s?[°ºO0]\.?\s*\S+", "", entry["versions"][language]["title"]).split()

    # Only a title with no body of its own (no "We, …"): a contents list waiting for its law.
    return all(
        not first["versions"][language]["has_body"]
        and first["versions"][language]["end"] == second["versions"][language]["start"]
        and words(first, language) == words(second, language)
        for language in shared
    )


# --- structure ----------------------------------------------------------------------------------------


# Numbered items within an article: "1º", "2°", "(1)", "(a)", "a)"; a text layer sometimes gives the
# ordinal sign as a plain "o" set close to the number ("1o").
ITEM = re.compile(r"^(\d{1,2}\s?[º°]|\d{1,2}o(?=\s)|\(\d{1,2}\)|\([a-z]\)|[a-z]\))\s*(\S.*)$")


def _capitals(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) >= 0.8 * len(letters)


ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50}


def _number(word: str) -> str:
    """An article's number in figures: "mbere"/"One"/"premier" are 1, "XIV" is 14."""
    if re.fullmatch(ONE, word, re.I):
        return "1"
    if re.fullmatch(ROMAN, word):
        values = [ROMAN_VALUES[c] for c in word]
        return str(sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values)))
    return word


def structure(lines: list[dict], language: str, part: str = "heading") -> list[dict]:
    """Nodes of one language version: the heading, the contents, the preamble, divisions, articles
    and their paragraphs (a paragraph ends where the line spacing opens up). An annex starts in its
    body (part="body"): it has no contents list or preamble of its own to wait for."""
    nodes: list[dict] = []
    previous = None
    for line in lines:
        if line.get("table"):
            nodes.append(
                {"kind": "table", "caption": "", **line["table"], "page": line["page"], "lines": [line]}
            )
            previous = line
            continue
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
        if (
            part == "heading"
            and nodes
            and nodes[-1]["kind"] == "heading"
            and previous is not None
            and (previous["page"] != line["page"] or len(nodes[-1]["text"]) > HEADING_MAX)
        ):
            # The title cannot go on: whatever opened the body was not recognised (a treaty's "The
            # State Parties", an order without a minister's line). Read the rest as the body.
            part = "body"
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
        # A number opens an item only where a new paragraph could start: after a gap, or after a line
        # ending ":" or ";". "… for a period of three" then "(3) years …" is one sentence wrapped.
        opens = not close or previous is None or previous["text"].rstrip().endswith((":", ";", "."))
        if node is None and part == "body" and opens:
            item = ITEM.match(text)
            if item:
                node = {
                    "kind": "list_item",
                    "level": "list_item",
                    "text": item.group(2),
                    "marker": item.group(1),
                    "ordered": True,
                    "page": line["page"],
                }
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
                last.setdefault("differences", []).extend(line.get("differences", []))
            elif wraps or (last is not None and last["kind"] == "paragraph" and close):
                last["text"] += " " + text
                last["lines"].append(line)
                last.setdefault("differences", []).extend(line.get("differences", []))
            else:
                kind = "heading" if part == "heading" else "paragraph"
                level = "heading-1" if part == "heading" else "paragraph"
                nodes.append(
                    {
                        "kind": kind,
                        "level": level,
                        "text": text,
                        "page": line["page"],
                        "lines": [line],
                        "differences": list(line.get("differences", [])),
                    }
                )
        else:
            node["lines"] = [line]
            node["differences"] = list(line.get("differences", []))
            nodes.append(node)
        previous = line
    for node in nodes:
        if node["kind"] != "table":
            node["text"] = re.sub(r"\s+", " ", node["text"]).strip()
    return nodes


def table_cells(node: dict) -> list[str]:
    return [cell["text"] for row in node.get("header_rows", []) + node.get("body_rows", []) for cell in row]


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


# The same printed sign read as two characters ("nº", "n°"; the apostrophe of "n’amategeko" read as the
# letter "ʼ"), and an OCR's Greek or Cyrillic look-alikes of Latin letters ("ΝΟΜΙΝΑTION", "КOAANA"):
# never differences of the text.
LOOK_ALIKE = str.maketrans(
    {"º": "°", "ª": "a", "ʼ": "’", "ʻ": "’", "‘": "’", "`": "’", "´": "’"}
    | dict(
        zip(
            "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧαβεικνορτυχАВЕКМНОРСТУХаеорсухі",
            "ABEZHIKMNOPTYXabeiknoptyxABEKMHOPCTYXaeopcyxi",
            strict=True,
        )
    )
)


# "Nº 12" however printed or read: the raised o a separate letter ("N o 12"), or run in ("No 12"); and
# the ordinal sign after a number, its o apart ("1 o Ms. …" for "1º Ms. …").
NUMBER_SIGN = re.compile(r"\b([Nn])(?:\s*[o°]|°)(?=\s*\d)")
ORDINAL = re.compile(r"\b(\d{1,3})\s?o\b")
# Below this mean confidence of its words, Google's OCR of a line is no check: the image is too unclear
# there (a smudged or faint print it misreads as other words), and the line is flagged as unchecked.
CLEAR = 0.6


def _checked_words(text: str) -> list[str]:
    from ingestion.native import words

    return words(ORDINAL.sub(r"\1°", NUMBER_SIGN.sub(r"\1°", text.translate(LOOK_ALIKE))))


def check_text_layer(
    nodes: list[dict], ocr_pages: dict[int, dict], sizes: dict[int, tuple[float, float]]
) -> set[int]:
    """Born-digital pages checked against an independent reading (CPR-01, DIG-01): each printed line the
    text layer gave, against the words Google's OCR read inside the same box on the page image. A unit
    whose lines differ is flagged as a scanned page's differences are: words read otherwise
    (``text_layer_differs``, medium; a number or negation among them sends the page to full review) and
    accents the page prints but the text layer lacks (``accents_missing``, low). A line the OCR read
    with a mean confidence below CLEAR is no check (``check_unclear``, low): the page is still not
    passed as agreed. A number or negation
    the OCR reads in a line that the layer lacks is a difference too; other words only it reads are
    not (specks, the next column's edge). The text stays the layer's: the check never changes a word.
    Table cells are not checked: a table's page is reviewed whole. Returns the pages checked."""
    from ingestion.compare import NEGATIONS
    from ingestion.native import align

    checked: set[int] = set()
    for node in nodes:
        worded, accents, unclear = [], [], []
        for line in node.get("lines", []):
            page = ocr_pages.get(line["page"])
            if page is None or line.get("table"):
                continue
            checked.add(line["page"])
            x0, y0, x1, y1 = _box([line], sizes[line["page"]])
            pad = (y1 - y0) * 0.3
            found = [
                w
                for w in page["words"]
                if x0 - 0.003 <= (w["x0"] + w["x1"]) / 2 <= x1 + 0.003
                and y0 - pad <= (w["y0"] + w["y1"]) / 2 <= y1 + pad
            ]
            mine = _checked_words(line["text"])
            if not mine:
                continue
            seen = [w["text"] for w in found]
            missing, extra = align(_checked_words(" ".join(seen)), mine)["unmatched"]
            # A word both readings have, read in another order (a neighbouring line's edge), is no difference.
            missing, extra = (
                list((Counter(missing) - Counter(extra)).elements()),
                list((Counter(extra) - Counter(missing)).elements()),
            )
            risky = [w for w in extra if w.isdigit() or w in NEGATIONS]
            if not missing and not risky:
                continue
            if found and sum(w.get("confidence", 1.0) for w in found) / len(found) < CLEAR:
                unclear.append((line["text"], " ".join(seen)))
            elif missing and sorted(map(_fold, missing)) == sorted(map(_fold, extra)):
                accents.append((" ".join(missing), " ".join(extra)))
            else:
                worded.append((" ".join(missing) or "∅", " ".join(extra) or "∅"))
        for kind, severity, group in (
            ("accents_missing", "low", accents),
            ("check_unclear", "low", unclear),
            ("text_layer_differs", "medium", worded),
        ):
            if group:
                node.setdefault("flags", []).append(
                    {
                        "severity": severity,
                        "kind": kind,
                        "primary": "; ".join(kept for kept, _ in group)[:400],
                        "others": {"Google OCR": "; ".join(other for _, other in group)[:400]},
                    }
                )
    return checked


def reading(
    nodes: list[dict],
    furniture_by_page: dict[int, list[str]],
    source_sha256: str,
    checks: dict,
    sizes: dict[int, tuple[float, float]] | None = None,
    scanned: set[int] | frozenset = frozenset(),
    one_reading: set[int] | frozenset = frozenset(),
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
        if number in scanned:
            # The page is a picture; its text was laid over it by someone else's OCR.
            flags.append(
                {
                    "severity": "medium",
                    "kind": "borrowed_ocr",
                    "primary": "",
                    "checks": {},
                    "compare": True,
                    "tokens": [0, 0],
                }
            )
        elif number in one_reading:
            # The page is a picture our OCR read; no second reading has checked its words yet.
            flags.append(
                {
                    "severity": "medium",
                    "kind": "one_reading",
                    "primary": "",
                    "checks": {},
                    "compare": True,
                    "tokens": [0, 0],
                }
            )
        for node in pages[number]:
            marker = _tokens(node.get("marker"))
            words = (
                sum(_tokens(cell) for cell in table_cells(node)) + _tokens(node.get("caption"))
                if node["kind"] == "table"
                else _tokens(node["text"])
            )
            if node.get("differences"):
                # A scanned page: Document AI's words, the OCR layer's where it read otherwise. Accents
                # restored are listed apart (low); words read differently are what to check (medium).
                # Each difference: the word kept, the other reading's, and which reading that was.
                changed = [tuple(d) if len(d) == 3 else (*d, "OCR layer") for d in node["differences"]]
                accents = [d for d in changed if _fold(d[0]) == _fold(d[1])]
                # Words our OCR was unsure of and the second reading did not see, left out: mostly the
                # text of the other side of the sheet showing through, stamps, specks (low).
                dropped = [d for d in changed if not d[0] and d[2] == "OCR" and _fold(d[0]) != _fold(d[1])]
                worded = [d for d in changed if _fold(d[0]) != _fold(d[1]) and d not in dropped]
                for kind, severity, group in (
                    ("accents_restored", "low", accents),
                    ("unsure_words_dropped", "low", dropped),
                    ("reading_differs", "medium", worded),
                ):
                    if group:
                        others: dict[str, list[str]] = {}
                        for _, other, reader in group:
                            others.setdefault(reader, []).append(other or "∅")
                        node.setdefault("flags", []).append(
                            {
                                "severity": severity,
                                "kind": kind,
                                "primary": "; ".join(kept or "∅" for kept, _, _ in group)[:400],
                                "others": {
                                    reader: "; ".join(words)[:400] for reader, words in others.items()
                                },
                            }
                        )
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
                    if k not in ("article", "division", "unusual", "flags", "lines", "differences")
                }
            )
        page_nodes = [
            {"kind": "furniture", "text": text, "page": number} for text in furniture_by_page.get(number, [])
        ] + page_nodes
        out.append(
            {
                "page": number,
                "nodes": page_nodes,
                "text": "\n".join(
                    " ".join(table_cells(n)) if n["kind"] == "table" else n["text"]
                    for n in page_nodes
                    if n["kind"] != "furniture"
                ),
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
                                "text": " ".join(
                                    filter(
                                        None,
                                        [node.get("marker"), node.get("text") or " ".join(table_cells(node))],
                                    )
                                ),
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
                            if ln["page"] == number and not ln.get("table")
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
# Who adopted it, as the enacting words print it: "THE PARLIAMENT HAS ADOPTED AND WE SANCTION, PROMULGATE
# …", "THE TRANSITIONAL NATIONAL ASSEMBLY HAD ADOPTED AND WE SANCTION …" (in capitals, as printed).
ADOPTED_BY = re.compile(r"\b(?:THE\s+)?([A-Z]+(?:\s+[A-Z]+){0,4})\s+HA[SD]\s+ADOPTED\b")
IN_FORCE_ON_PUBLICATION = re.compile(
    r"(?:comes|shall come|enters) into force on the date of its publication", re.I
)
# --- what an instrument does to the earlier ones it names --------------------------------------------
# "Law nº 028/2023 of 19/05/2023 on … is repealed" repeals that law. "Article 44 of Law n° 66/2018 of
# 30/08/2018 … is repealed" and "Article 65 of Law nº 32/2016 … is amended as follows" amend it, and so
# does a title "… amending Law n° 66/2018 …". An order's preamble names the law it is made under:
# "Pursuant to Law n° 70/2019 …, especially in Articles 4 and 9;". Read in English, else in French,
# else in Kinyarwanda ("Itegeko nº 028/2023 ryo ku wa 19/05/2023 … rivanyweho").
CITED = re.compile(
    r"\b(Organic\s+Law|Decree[-\s]Law|Law|Presidential\s+Order|Prime\s+Minister[’']s\s+Order"
    r"|Ministerial\s+Order|Loi\s+organique|D[ée]cret[-\s]loi|Loi|Arr[êe]t[ée]\s+pr[ée]sidentiel"
    r"|Arr[êe]t[ée]\s+du\s+Premier\s+Ministre|Arr[êe]t[ée]\s+minist[ée]riel"
    r"|Itegeko\s+Ngenga|Itegeko[-\s]Teka|Itegeko|Iteka\s+rya\s+Perezida"
    r"|Iteka\s+rya\s+Minisitiri\s+w[’']\s?Intebe|Iteka\s+rya\s+Minis(?:i)?tiri)"
    r"\s+n\s?[°ºᵒo]\.?\s*(\d[\w./]*(?:\s*(?:bis|ter)\b)?(?:\s*/\s*[\w./]+)?)"
    # "of 19/05/2023", "du 26/07/2000", "ryo ku wa 19/05/2023", "ryo kuwa 26 Nyakanga 2000"
    r"\s+(?:of|du|ryo\s+ku\s?wa)\s+(?:\d{1,2}[/.]\d{1,2}[/.]\s?|\d{1,2}(?:er|st|nd|rd|th)?\s+[^\W\d_]+\s+)"
    r"((?:19|20)\d\d)\b",
    re.I,
)
CITED_KIND = {
    "organic law": "organic_law",
    "loi organique": "organic_law",
    "itegeko ngenga": "organic_law",
    "decree law": "decree_law",
    "decret loi": "decree_law",
    "itegeko teka": "decree_law",
    "law": "law",
    "loi": "law",
    "itegeko": "law",
    "presidential order": "presidential_order",
    "arrete presidentiel": "presidential_order",
    "iteka rya perezida": "presidential_order",
    "prime minister's order": "prime_ministers_order",
    "arrete du premier ministre": "prime_ministers_order",
    "iteka rya minisitiri w'intebe": "prime_ministers_order",
    "ministerial order": "ministerial_order",
    "arrete ministeriel": "ministerial_order",
    "iteka rya minisitiri": "ministerial_order",
    "iteka rya ministiri": "ministerial_order",
}
# What is done to the instrument named before it: "is repealed", "are hereby repealed", "is amended
# as follows", "est abrogée", "sont modifiés", "rivanyweho", "ihinduwe ku buryo bukurikira",
# "ryongewemo ingingo ya 8" (has an article added).
ACTION = re.compile(
    r"\b(?:is|are)\s+(?:hereby\s+)?(repealed|amended|modified|complemented|replaced)\b"
    r"|\b(?:est|sont)\s+(abrog|modifi|compl[ée]t|remplac)\w*"
    r"|\b(?:ri|i|zi|ry|y|z)?(vany?weho|hinduwe|ongewemo|ujujwe)\b",
    re.I,
)
# Where a sentence or an article's heading ends ("No. 5" and "n°." do not end one).
SUBJECT_START = re.compile(r"(?<![Nn][Oo°º])[.;:»”]\s+(?=[A-ZÀ-Ý«“\"])")
# The article a repealing sentence is in, its heading just before it: "Article 64: ", "Ingingo ya 75: ".
ARTICLE_HEADING = re.compile(r"\b(?:Article|Ingingo\s+ya)\s+\w+(?:\s+(?:bis|ter))?\s*:\s*$", re.I)
SENTENCE_END = re.compile(r"(?<![Nn][Oo°º])[.;](?=\s|$)")
_PART = (
    r"articles?|paragraphs?|sub-?paragraphs?|items?|points?|chapters?|sections?|annex(?:es)?|schedules?"
    r"|provisions?|alin[ée]as?|paragraphes?|chapitres?|annexes?|dispositions?"
    r"|ingingo|igika|ibika|umutwe|imitwe|icyiciro|umugereka|imigereka"
)
# A part of the instrument named after it: "Article 44 of", "Paragraph 2 of Article 5 of the",
# "L’article 44 de la", "Ingingo ya 44 y’".
PART_OF = re.compile(
    rf"\b(?:{_PART})\b[^.;]{{0,80}}?"
    r"(?:\b(?:of|de|du)\s+(?:the\s+|la\s+|l[’']\s?)?|\b(?:[yzw]|by|cy|ry|rw)[’']\s?)$",
    re.I,
)
# A further instrument acted on is named after a separator ("…, and Law n° …"); one named without
# is part of the previous one's title ("Order n° 5 … implementing Law n° 59/2011 …").
ANOTHER = re.compile(r"(?:,|;|\band\b|\bet\b|\bna\b|\bn[’'])\s*(?:the\s+|la\s+|l[’']\s?)?$", re.I)
# A law named only as what changed the one being acted on: "Law n° 5/2001, as amended by Law n° 9/2005".
CHANGED_BY = re.compile(r"\b(?:by|par)\s+(?:the\s+|la\s+|l[’']\s?)?$|\bn[’']\s?$", re.I)
TITLE_ENDS = re.compile(
    r",?\s+(?:as\s+(?:amended|modified|complemented)|telle?s?\s+que|nk[’']\s?uko\s+ry)"
    rf"|\s*(?:,|\band\b|\bet\b|\bn[’'])\s*(?=(?:the\s+|la\s+|l[’']\s?)?(?:{_PART})\b)"
    r"|,?\s+(?:and\s+)?(?:all|any)\s+(?:other\s+)?prior\b|,\s+(?:its|their)\s+other\b|,?\s+as\s+well\s+as\b"
    r"|,\s+confirmed\s+by\b|,?\s+(?:especially|sp[ée]cialement|cyane\s+cyane)\b",
    re.I,
)
# "Save Article 163 of Law n° 13/2009 …, its other provisions … are repealed": repealed but for a part.
SAVING = re.compile(
    r"\b(?:save|except|with\s+the\s+exception\s+of|sauf|hormis|[àa]\s+l[’']exception\s+de|uretse|keretse)\b",
    re.I,
)
ARTICLE_REF = re.compile(
    r"\b(?:articles?|ingingo(?:\s+ya)?)\s+(\d+(?:\s?(?:bis|ter|quater))?|one|premier|mbere)\b", re.I
)
AMENDING = re.compile(
    r"\b(?:amending|modifying|complementing|modifiant|compl[ée]tant|rihindura|ihindura|ryuzuza|ryunganira)\b",
    re.I,
)
REPEALING = re.compile(r"\b(?:repealing|abrogeant|rivanaho|ivanaho|rikuraho|ikuraho)\b", re.I)
# The law an instrument is made under, named first after "Pursuant to", "Vu", "Ashingiye ku".
MADE_UNDER = re.compile(
    r"\b(?:Pursuant\s+to|Based\s+on|Vu|Ashingiye\s+ku|Ishingiye\s+ku)\s+(?:the\s+|la\s+|le\s+|l[’']\s?)?",
    re.I,
)
# The articles it relies on: "especially in Articles 4, 9 and 13", "spécialement en ses articles 8,
# 9 et 10", "cyane cyane mu ngingo yaryo ya 23" (up to a paragraph: "igika cya 3" is not an article).
RELIED_ON = re.compile(
    r"(?:especially\s+in(?:\s+its)?|sp[ée]cialement\s+en\s+(?:son|ses)|cyane\s+cyane\s+mu)\s+"
    r"(?:articles?|ngingo)\s+([^;]*)",
    re.I,
)
NUMBERED = re.compile(r"\d+(?:\s?(?:bis|ter|quater)\b)?|\b(?:one|premier|mbere)\b", re.I)


def _plain_words(words: str) -> str:
    """'Arrêté  Présidentiel' -> 'arrete presidentiel', for CITED_KIND."""
    plain = unicodedata.normalize("NFKD", words.replace("’", "'")).encode("ascii", "ignore").decode()
    return " ".join(plain.lower().replace("-", " ").replace("' ", "'").split())


def number_key(number: str) -> str:
    """A number as compared: "043/2016" and "43/2016" alike, "53bis/2013" and "53 bis/2013" alike."""
    tokens = re.findall(r"\d+|[a-z]+", number.lower())
    return "-".join(t.lstrip("0") or "0" if t.isdigit() else t for t in tokens)


def _article(ref: str) -> str:
    """'One', 'premier' and 'mbere' are Article 1; '7 TER' is '7 ter'."""
    ref = " ".join(ref.lower().split())
    return "1" if ref in ("one", "premier", "mbere") else re.sub(r"(\d)(bis|ter|quater)", r"\1 \2", ref)


def _articles(refs: list[str]) -> str | None:
    """'Article 44' or 'Articles 43, 44 and 57' (the first ten, then how many more)."""
    refs = list(dict.fromkeys(refs))
    if not refs:
        return None
    if len(refs) == 1:
        return f"Article {refs[0]}"
    shown, more = refs[:10], len(refs) - 10
    if more > 0:
        return f"Articles {', '.join(shown)} and {more} more"
    return f"Articles {', '.join(shown[:-1])} and {shown[-1]}"


def _printed(text: str) -> str:
    """A citation as printed, its title cut where the sentence goes on to something else."""
    end = TITLE_ENDS.search(text)
    text = re.sub(r"\s+", " ", text[: end.start()] if end else text).strip(" ,;:")
    text = re.sub(r"\s+(?:and|et)$", "", text)
    return (text[:1].upper() + text[1:])[:300]


def _relied_on(clause: str) -> list[str]:
    """The articles a preamble says it relies on, up to the first paragraph it names."""
    found = RELIED_ON.search(clause)
    if not found:
        return []
    articles = re.split(
        r"\b(?:igika|ibika|paragraphs?|paragraphes?|alin[ée]as?|items?|points?)\b", found.group(1)
    )[0]
    return [_article(m.group(0)) for m in NUMBERED.finditer(articles)]


def cited(versions: dict[str, list[dict]], entry: dict) -> list[dict]:
    """The earlier instruments this one repeals or amends, and those it is made under, each by its
    citation as printed, with the kind, number and year it is found by and, where only some articles
    change or are relied on, which. An instrument it repeals comes with the provision that does, as
    printed ("words", with its article heading, of at most 400 characters), and the page it begins on."""
    language = next((code for code in ("en", "fr", "rw") if versions.get(code)), None)
    if language is None:
        return []
    own = (entry["kind"], number_key(entry["number"]))
    found: dict[tuple, dict] = {}

    def add(match, kind, target, refs=(), only_new=False, saved=False, provision=None):
        instrument = CITED_KIND.get(_plain_words(match.group(1)))
        number, year = match.group(2).rstrip("./"), match.group(3)
        # What it does to another instrument, and what it is made under, are kept apart.
        key = (instrument, number_key(number), year, kind == "made_under")
        if instrument is None or key[:2] == own or (only_new and key in found):
            return
        link = found.setdefault(
            key,
            {
                "kind": kind,
                "target": target,
                "instrument": instrument,
                "number": number,
                "year": year,
                "refs": [],
                "saved": [],
            },
        )
        if kind == "repeals":
            link["kind"] = "repeals"  # the whole instrument, though some sentence amends a part first
            if provision and "words" not in link:
                link |= provision
        link["saved" if saved else "refs"] += list(refs)

    lines = versions[language]
    text = " ".join(line["text"] for line in lines)
    begins = list(accumulate((len(line["text"]) + 1 for line in lines[:-1]), initial=0))

    def printed(start: int, end: int) -> dict | None:
        """The words of the text from ``start`` to ``end``, with the page they begin on."""
        words = " ".join(text[start:end].split())
        page = lines[bisect_right(begins, start) - 1].get("page")
        return {"words": words, **({"page": page} if page else {})} if len(words) <= 400 else None

    for action in ACTION.finditer(text):
        base = max(0, action.start() - 600)
        window = text[base : action.start()]
        starts = list(SUBJECT_START.finditer(window))
        subject = window[starts[-1].end() :] if starts else window
        provision = None
        if starts or not base:  # where its sentence begins, and the article heading before it
            begin = base + (starts[-1].end() if starts else 0)
            article = ARTICLE_HEADING.search(text, max(0, begin - 40), begin)
            stop = SENTENCE_END.search(text, action.end())
            provision = printed(article.start() if article else begin, stop.end() if stop else action.end())
        citations = list(CITED.finditer(subject))
        verb = next(v for v in action.groups() if v).lower()
        for i, match in enumerate(citations):
            before = subject[citations[i - 1].end() if i else 0 : match.start()]
            if CHANGED_BY.search(before):
                continue
            part = PART_OF.search(before)
            if i and not part and not ANOTHER.search(before):
                continue
            saved = bool(part and SAVING.search(before))
            if verb.startswith(("repeal", "abrog", "van")) and (saved or not part):
                kind = "repeals"
            elif part:
                kind = "amends"
            elif verb.startswith(("replac", "remplac")):
                continue  # "Law n° … is replaced by this Law": left for the reviewer
            else:
                kind = "amends"
            end = citations[i + 1].start() if i + 1 < len(citations) else len(subject)
            refs = [_article(m.group(1)) for m in ARTICLE_REF.finditer(before)] if part else []
            add(match, kind, _printed(subject[match.start() : end]), refs, saved=saved, provision=provision)
    # The preamble: the law it is made under, named straight after "Pursuant to".
    for basis in MADE_UNDER.finditer(text):
        match = CITED.match(text, basis.end())
        if match:
            clause = text[match.start() : match.start() + 600].split(";")[0]
            add(match, "made_under", _printed(clause), _relied_on(clause))
    # The title: "Law n° 027/2023 of 18/05/2023 amending the Law n° 66/2018 of 30/08/2018 …".
    heading = entry["versions"].get(language) or {}
    title = sentence_case(heading.get("title") or "")
    as_printed = " ".join((heading.get("title") or "").split())
    named = {"words": as_printed, **({"page": heading["pages"][0]} if heading.get("pages") else {})}
    named = named if len(as_printed) <= 400 else None
    citations = list(CITED.finditer(title))
    for i, match in enumerate(citations):
        before = title[citations[i - 1].end() if i else 0 : match.start()]
        kind = "repeals" if REPEALING.search(before) else "amends" if AMENDING.search(before) else None
        if kind:  # what the text says comes first: its citation is printed in full
            end = citations[i + 1].start() if i + 1 < len(citations) else len(title)
            add(match, kind, _printed(title[match.start() : end]), only_new=True, provision=named)
    out = []
    for link in found.values():
        refs, saved = link.pop("refs"), link.pop("saved")
        if link["kind"] in ("amends", "made_under"):
            note = _articles(refs)
        else:
            note = f"Except {_articles(saved)}" if saved else None
        out.append({**link, **({"note": note} if note else {})})
    return out


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


def stated(paragraphs: list[dict], pattern: re.Pattern, stops: tuple[str, ...] = (". ",)) -> dict | None:
    """Where the text states something: the page of the paragraph that says it and the sentence that
    does, as printed (a paragraph may run on into the signatures); with ``stops`` (". ", "; "), the clause
    of a preamble that does. None when no paragraph says it in a sentence of at most 400 characters."""
    for node in paragraphs:
        words = " ".join(node["text"].split())
        found = pattern.search(words)
        if not found:
            continue
        start = max(words.rfind(stop, 0, found.start()) for stop in stops) + 1
        ends = [end for end in (words.find(stop, found.end()) for stop in stops) if end >= 0]
        sentence = words[start : min(ends) + 1 if ends else None].strip()
        return {"page": node["page"], "evidence": sentence} if len(sentence) <= 400 else None
    return None


GAZETTE = "Official Gazette of the Republic of Rwanda"


@cache
def gazette_publisher() -> str | None:
    """The Gazette's publisher as Rwanda's reviewed jurisdiction profile names it (schema/jurisdictions.json),
    never typed here a second time."""
    profile = registry()["rw"]
    return next((g.publisher for g in profile.gazettes if g.name == GAZETTE), None)


def suggest(versions: dict[str, list[dict]], entry: dict, issue: dict) -> tuple[dict, dict, dict]:
    """Details of the record read from the instrument as printed: its title, number and dates, who
    adopted it and the sittings that did, who promulgated, countersigned and sealed it, the date it was
    promulgated, and where it was published; the reviewer confirms each against the page.

    And apart from them, each with the words that state it and their page, a suggestion until a reviewer
    chooses it (owner decision 2026-10-21), the record's own fields left empty:
    - the legal facts the text suggests: when it came into force, and what each language version is
      worth as law from the language it was adopted in;
    - what is inferred, not printed for its step: the dates it was countersigned and sealed, for the
      Gazette prints one date above the three signatures ("Kigali, on 19/09/2019"); and, for a law
      whose preamble names only the Chamber of Deputies' sitting, that the Senate did not sit on it."""
    english = [line["text"] for line in versions.get("en", [])]
    text = " ".join(english)
    paragraphs = [n for n in versions_nodes(versions.get("en", [])) if n["kind"] == "paragraph"]
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
    adopted_by = ADOPTED_BY.search(text)
    if adopted_by and entry["kind"] in ("law", "organic_law", "constitution"):
        values["issued_by"] = adopted_by.group(1).title()  # "Parliament", "Transitional National Assembly"
    inferred: dict = {}
    for step, pattern in SITTING.items():
        match = pattern.search(text)
        if match and spoken_date(match.group(1)):
            values[f"step:adopted:{step}.date"] = spoken_date(match.group(1))
    sittings = {step for step in SITTING if f"step:adopted:{step}.date" in values}
    if entry["kind"] == "law" and sittings == {"deputies"}:
        # A law whose preamble names only the Chamber of Deputies' sitting: the Senate did not sit on it.
        deputies = stated(paragraphs, SITTING["deputies"], stops=(". ", "; "))
        if deputies:
            inferred["step:adopted:senate.date"] = {"absent": "not_applicable", **deputies}
    every = [line for lines in versions.values() for line in lines]
    signed = next((line for line in every if KIGALI.search(line["text"])), None)
    if signed:
        found = KIGALI.search(signed["text"])
        when = f"{found.group(3)}-{int(found.group(2)):02d}-{int(found.group(1)):02d}"
        values["step:promulgated.date"] = when  # the President signs under the date
        kigali = {"value": when, "page": signed.get("page"), "evidence": found.group(0)}
        for step in ("countersigned", "sealed"):  # the other two signatures carry no date of their own
            inferred[f"step:{step}.date"] = dict(kigali)
    for language in ("en", *[k for k in versions if k != "en"]):
        people = signatories([line["text"] for line in versions.get(language, [])])
        for step, person in people.items():
            values.setdefault(f"step:{step}.person", person)
    legal: dict = {}
    if issue.get("date"):
        values["step:published.date"] = issue["date"]
        values["publication.date"] = issue["date"]
        in_force = stated(paragraphs, IN_FORCE_ON_PUBLICATION)
        if in_force:
            legal["step:commenced.date"] = {"value": issue["date"], **in_force}
            if values["identity.family"] in ("legislation", "constitution"):
                legal["profile.commencement"] = {"value": "on_publication", **in_force}
    values["publication.kind"] = "gazette"
    values["publication.name"] = GAZETTE
    edition = "Special" if issue.get("special") else str(issue.get("number") or "")
    edition += " Bis" if issue.get("bis") else (" Ter" if issue.get("ter") else "")
    if edition.strip():
        values["publication.number"] = edition.strip()
    values["publication.publisher"] = gazette_publisher()
    values["text.form"] = "gazette_publication"
    values["expressions"] = [
        {"language": language, "script": "Latn", "direction": "ltr"}
        for language in LANGUAGES
        if language in entry["versions"]
    ]
    # Adopted in Kinyarwanda (or English): that version authoritative, the others official translations,
    # each with the paragraph that says so and its page.
    adopted = ADOPTED_IN.search(text)
    authority = "rw" if adopted and adopted.group(1).lower() in ("ikinyarwanda", "kinyarwanda") else None
    if adopted and adopted.group(1).lower() == "english":
        authority = "en"
    said = stated(paragraphs, ADOPTED_IN) if authority else None
    if said:
        legal["expressions"] = {
            "authority": {
                row["language"]: {
                    "value": "authoritative" if row["language"] == authority else "official_translation",
                    **said,
                }
                for row in values["expressions"]
            }
        }
    purpose = next(
        (n for n in paragraphs if n.get("after_article") == "1" and n.get("purpose")),
        None,
    )
    if purpose:
        values["text.summary.text"] = purpose["text"]
    return values, legal, inferred


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


# --- scanned pages: the layout of the OCR layer, the words of Document AI --------------------------------


def _fold(token: str) -> str:
    """A word for matching: no accents, case or punctuation ("Désireux," and "Desireux" match)."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", plain)


def docai_columns(reading: dict, languages: int) -> list[str]:
    """Document AI's text of a page, as one text per language column: a page it read as a table of
    three columns gives each column's cells in order; otherwise its nodes in reading order."""
    nodes = [n for n in reading.get("nodes") or [] if n.get("kind") != "furniture"]
    tables = [n for n in nodes if n.get("kind") == "table"]
    if languages == 3 and tables:
        widest = max(tables, key=lambda t: sum(len(r) for r in t["header_rows"] + t["body_rows"]))
        rows = widest["header_rows"] + widest["body_rows"]
        if rows and max(len(r) for r in rows) == 3:
            return [" ".join(r[k]["text"] for r in rows if len(r) > k) for k in range(3)]
    parts = []
    for node in nodes:
        if node.get("kind") == "table":
            parts += [cell["text"] for row in node["header_rows"] + node["body_rows"] for cell in row]
        else:
            parts.append(" ".join(filter(None, [node.get("marker"), node.get("text")])))
    return [" ".join(parts)]


# A paragraph's number or letter ("a)", "3.", "(1)", "l.", "1º"): Document AI's layout reading leaves
# such numbering out of its text, so where only the OCR layer has it, it is kept.
MARKER_LIKE = re.compile(r"^[(\[]?(?:\d{1,3}|[a-zA-Z]{1,3}|[ivxlc]{1,5})[.)\]º°]?\)?$")


def correct_lines(lines: list[dict], better: str) -> list[dict]:
    """The lines of a column with their words checked against a second reading of the same text
    (the Layout Parser's): the two word sequences are aligned, and each line keeps a note of every
    difference (the word kept, the other reading's, and which reading that was).

    Which word is kept: on lines our OCR read, its words stand where it was sure of them, so the
    page reads as printed (the Gazette's own slips, its numbering); where it was unsure (broken or
    faint type), the second reading's words are taken. On lines of someone else's OCR layer, the
    second reading's words are taken, the apostrophes and numbering as printed kept."""
    import difflib

    owner, borrowed = [], []
    for k, line in enumerate(lines):
        if line.get("table"):
            continue
        for word in line["text"].split():
            owner.append(k)
            borrowed.append(word)
    good = better.split()
    matcher = difflib.SequenceMatcher(
        None, [_fold(w) for w in borrowed], [_fold(w) for w in good], autojunk=False
    )
    words: dict[int, list[str]] = {k: [] for k in range(len(lines))}
    notes: dict[int, list[tuple[str, str, str]]] = {k: [] for k in range(len(lines))}

    def letters(text: str) -> str:  # what a reader would call a different word: letters and accents
        return re.sub(r"[^\w]", "", text.lower())

    def same_glyphs(a: str, b: str) -> bool:  # only the apostrophe or quotation marks differ
        return re.sub(r"[’‘`´']", "'", a) == re.sub(r"[’‘`´']", "'", b)

    def sure(i: int) -> bool:  # a word our OCR read and was sure of
        line = lines[owner[i]]
        return "doubtful" in line and borrowed[i] not in line["doubtful"]

    def base(i: int) -> str:  # the name of the reading the lines come from
        return "OCR" if "doubtful" in lines[owner[i]] else "OCR layer"

    opcodes = matcher.get_opcodes()
    # Words the OCR has and the second reading puts elsewhere (columns taken in another order):
    # where the OCR's sure words are kept, the second reading's copy of them is not added again.
    kept_elsewhere = {
        _fold(borrowed[i]) for op, i1, i2, _, _ in opcodes if op == "delete" for i in range(i1, i2) if sure(i)
    }
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                if same_glyphs(good[j], borrowed[i]):
                    words[owner[i]].append(borrowed[i])  # the apostrophe as printed
                elif sure(i):
                    words[owner[i]].append(borrowed[i])
                    if letters(good[j]) != letters(borrowed[i]):
                        notes[owner[i]].append((borrowed[i], good[j], "Layout Parser"))
                else:
                    words[owner[i]].append(good[j])
                    if letters(good[j]) != letters(borrowed[i]):
                        notes[owner[i]].append((good[j], borrowed[i], base(i)))
        elif op == "replace" and i2 - i1 == j2 - j1:
            # As many words on each side: each pair decided on its own.
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                if sure(i):
                    words[owner[i]].append(borrowed[i])
                    if letters(good[j]) != letters(borrowed[i]):
                        notes[owner[i]].append((borrowed[i], good[j], "Layout Parser"))
                else:
                    words[owner[i]].append(good[j])
                    if letters(good[j]) != letters(borrowed[i]):
                        notes[owner[i]].append((good[j], borrowed[i], base(i)))
        elif op == "replace" and all(sure(i) for i in range(i1, i2)):
            for i in range(i1, i2):
                words[owner[i]].append(borrowed[i])
            new, old = " ".join(good[j1:j2]), " ".join(borrowed[i1:i2])
            if letters(new) != letters(old):
                notes[owner[i1]].append((old, new, "Layout Parser"))
        elif op in ("replace", "insert"):
            home = owner[i1] if i1 < len(owner) else (owner[i1 - 1] if i1 else 0)
            added = good[j1:j2]
            if (
                op == "insert"
                and added
                and sum(_fold(w) in kept_elsewhere for w in added) >= 0.8 * len(added)
            ):
                continue  # the same words, which the OCR read in another place and keeps there
            if op == "replace":
                # Numbering only the OCR has (the Layout Parser leaves it out) stays with its line.
                for i in range(i1, i2):
                    if MARKER_LIKE.match(borrowed[i]) and _fold(borrowed[i]) not in {_fold(w) for w in added}:
                        words[owner[i]].append(borrowed[i])
            words[home].extend(added)
            new, old = " ".join(added), " ".join(borrowed[i1:i2])
            if letters(new) != letters(old):
                notes[home].append((new, old, base(i1) if i1 < len(owner) else "OCR"))
        else:  # words only the OCR has: kept where it was sure; else numbering kept, symbols not
            if all(sure(i) for i in range(i1, i2)):
                for i in range(i1, i2):
                    words[owner[i]].append(borrowed[i])
                # Numbering the Layout Parser leaves out is no difference; other words are.
                old = " ".join(w for w in borrowed[i1:i2] if not MARKER_LIKE.match(w))
                if letters(old):
                    notes[owner[i1]].append((old, "", "Layout Parser"))
                continue
            kept = [w for w in borrowed[i1:i2] if MARKER_LIKE.match(w)]
            words[owner[i1]].extend(kept)
            dropped = " ".join(w for w in borrowed[i1:i2] if w not in kept)
            if letters(dropped):
                notes[owner[i1]].append(("", dropped, base(i1)))
    out = []
    for k, line in enumerate(lines):
        if line.get("table"):
            out.append(line)
            continue
        out.append({**line, "text": " ".join(words[k]), "read_by": "docai", "differences": notes[k]})
    return out


def correct_page(page: dict, reading: dict) -> dict:
    """A scanned page, split as its OCR layer lays it out, with Document AI's words. A page with no
    OCR lines has nothing to place the words in: it is left as it is (read it with OCR first)."""
    columns = [c for c in page["columns"]]
    if not any(c["lines"] for c in columns):
        return page
    texts = docai_columns(reading, len(columns) if page["layout"] == "parallel" else 1)
    if len(texts) == len(columns):
        corrected = [
            {**c, "lines": correct_lines(c["lines"], t)} for c, t in zip(columns, texts, strict=True)
        ]
    else:
        # One text for the page: align it with the columns' lines taken in reading order.
        every = [line for c in columns for line in c["lines"]]
        fixed = correct_lines(every, texts[0])
        corrected, start = [], 0
        for c in columns:
            corrected.append({**c, "lines": fixed[start : start + len(c["lines"])]})
            start += len(c["lines"])
    return {**page, "columns": corrected, "read_by": "docai"}
