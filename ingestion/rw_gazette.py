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
# 2026-10-29.1: headings over two lines, more kinds, bodies known by their enacting words (orders with
# one title kept apart), notices recorded, annexes without their instrument kept, treaties' family.
# 2026-10-30.1: a file name's "57-2008" agrees with the printed "57/2008"; .2: evidence in the details file.
PIPELINE = "2026-10-30.5"
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

# A running head at the top of a page, however spelt, by the Gazette's name and the issue's date.
RUNNING_HEAD = re.compile(
    r"(?i)^(?:[-–—]?\s*\d{1,4}\s*[-–—]?\s*)?(?:o\w{3,9}\s+gazet\w*|igazeti|journal\s+officiel)\b"
    r".*\d{1,2}[/.]\d{1,2}[/.]\d{4}"
)
# Other words that head an instrument's contents and so end its title: Kinyarwanda's "IBIRIMO" and
# "AMASHAKIRO", and the Gazette's own "TABLE DES CONTENTS".
CONTENTS_WORDS = re.compile(r"(?i)^(?:i?birimo|amashakiro|table\s+des\s+contents)\b")

# The heading that opens an instrument, as printed in capitals in each language: its kind, then its
# number ("N° 11/2001"), or on older laws only its date ("LOI DU 19 FÉVRIER 1964", "ITEGEKO RYO KUWA
# 19 GASHYANTARE 1964"). Older kinds: decree-laws, ordinances, decrees, royal orders. A number
# printed "N° 001 BIS /2021" is the whole of it, not "001" (which is another law).
# Some PDFs print the sign with a symbol font's private character (U+F0B0): "LAW N\uf0b042/2011".
_NUMBER = r"\s+N\s?[°ºO0\uf0b0]\.?\s*(\S+?(?:\s*(?i:BIS|TER)\b)?(?:\s*/\s*\S+)?)(?=\s|$)"
# The kinds' words in each language, longest first where one begins another. A general regulation of
# the central bank is printed "AMABWIRIZA RUSANGE" and the Prime Minister's instructions "AMABWIRIZA YA
# MINISITIRI W'INTEBE" (2026-10-29: neither was recognised).
KIND_WORDS = {
    "rw": r"ITEGEKO NGENGA|ITEGEKO-TEKA|ITEGEKO|ITEKA RYA PEREZIDA|ITEKA RYA MINISITIRI W[’']INTEBE"
    r"|ITEKA RYA MINISITIRI|ITEKA RYA MINISTRI|AMABWIRIZA YA MINISITIRI W[’']INTEBE"
    r"|AMABWIRIZA YA PEREZIDA W[’']URUKIKO RW[’']IKIRENGA|AMABWIRIZA YA MINISITIRI|AMABWIRIZA RUSANGE|AMABWIRIZA",
    "en": r"ORGANIC LAW|DECREE-LAW|LAW|PRESIDENTIAL ORDER|PRIME MINISTER[’']S ORDER"
    r"|PRIME MINISTER[’']S INSTRUCTIONS?|MINISTERIAL ORDER|MINISTERIAL INSTRUCTIONS?"
    r"|PRACTICE DIRECTIONS? OF THE PRESIDENT OF THE SUPREME COURT"
    r"|PRACTICE DIRECTIONS?|REGULATIONS?|INSTRUCTIONS?|DECREE|ORDINANCE",
    "fr": r"LOI ORGANIQUE|D[ÉE]CRET-LOI|ORDONNANCE-LOI|ORDONNANCE L[ÉE]GISLATIVE|LOI|ARR[ÊE]T[ÉE] PR[ÉE]SIDENTIEL"
    r"|ARR[ÊE]T[ÉE] DU PREMIER MINISTRE|ARR[ÊE]T[ÉE] MINIST[ÉE]RIEL|ARR[ÊE]T[ÉE] ROYAL"
    r"|INSTRUCTIONS? DU PREMIER MINISTRE|INSTRUCTIONS? DU PR[ÉE]SIDENT DE LA COUR SUPR[ÊE]ME"
    r"|DIRECTIVES? PRATIQUES?|R[ÈE]GLEMENTS?|INSTRUCTIONS?|ORDONNANCE|D[ÉE]CRET",
}
_DATED = {"rw": r"\s+R?YO\s+KU\s?WA\s+(.*\d{4})", "en": r"\s+OF\s+(.*\d{4})", "fr": r"\s+DU\s+(.*\d{4})"}
# Kinyarwanda names whose instruction or regulation it is between the kind and its number ("AMABWIRIZA
# Y'UMWANDITSI MUKURU N° 01/2010/ORG", "AMABWIRIZA YA MINISITIRI N° 001/2014").
_QUALIFIER = {"rw": r"(?:\s+(?:YA|Y[’'])\s*[A-ZÀ-Ý’' -]{2,60}?)?", "en": "", "fr": ""}
OPENING = {
    language: re.compile(rf"^({words}){_QUALIFIER[language]}(?:{_NUMBER}|{_DATED[language]})")
    for language, words in KIND_WORDS.items()
}
# A line that begins a kind's words, in capitals, with no number: the heading may go on over the next
# line or two ("ARRETE" / "PRESIDENTIEL N°21/01 DU 12/7/2003 …").
KIND_HEAD = re.compile(
    r"^(?:ITEGEKO|ITEKA|AMABWIRIZA|ORGANIC|DECREE|LAW|PRESIDENTIAL|PRIME|MINISTERIAL|PRACTICE|REGULATIONS?|INSTRUCTIONS?"
    r"|ORDINANCE|LOI|D[ÉE]CRET|ORDONNANCE|ARR[ÊE]T[ÉE]|R[ÈE]GLEMENTS?|THE CONSTITUTION|CONSTITUTION"
    r"|LA CONSTITUTION)\b[A-ZÀ-Ý’' -]*$"
)
# A kind's words alone on their line, the number or date on the next ("ITEKA RYA MINISITIRI W'INTEBE",
# then "N°003/03 RYO KU WA 16/01/2015 …"): read as one heading. Before 2026-10-29 such a heading was
# not recognised, and the instrument was lost in that language.
KIND_ALONE = {language: re.compile(rf"^(?:{words})\s*$") for language, words in KIND_WORDS.items()}
# A constitution is printed without a number, its heading sometimes over two lines ("THE CONSTITUTION
# OF THE" then "REPUBLIC OF RWANDA OF 2003 REVISED IN 2015").
CONSTITUTION = {
    "rw": re.compile(r"^ITEGEKO NSHINGA\b"),
    "en": re.compile(r"^(?:THE\s+)?CONSTITUTION OF THE\s+REPUBLIC\b"),
    "fr": re.compile(r"^(?:LA\s+)?CONSTITUTION DE LA\s+R[ÉE]PUBLIQUE\b"),
}
CONSTITUTION_BEGUN = {
    "rw": re.compile(r"^ITEGEKO\s*$"),
    "en": re.compile(r"^(?:THE\s+)?CONSTITUTION OF THE\s*$"),
    "fr": re.compile(r"^(?:LA\s+)?CONSTITUTION DE LA\s*$"),
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
    "AMABWIRIZA RUSANGE": "regulations",
    "AMABWIRIZA YA MINISITIRI W’INTEBE": "instructions",
    "PRIME MINISTER’S INSTRUCTIONS": "instructions",
    "PRIME MINISTER’S INSTRUCTION": "instructions",
    "INSTRUCTIONS DU PREMIER MINISTRE": "instructions",
    "INSTRUCTION DU PREMIER MINISTRE": "instructions",
    "AMABWIRIZA YA PEREZIDA W’URUKIKO RW’IKIRENGA": "practice_direction",
    "AMABWIRIZA YA MINISITIRI": "instructions",
    "MINISTERIAL INSTRUCTIONS": "instructions",
    "MINISTERIAL INSTRUCTION": "instructions",
    "PRACTICE DIRECTIONS OF THE PRESIDENT OF THE SUPREME COURT": "practice_direction",
    "PRACTICE DIRECTION OF THE PRESIDENT OF THE SUPREME COURT": "practice_direction",
    "PRACTICE DIRECTIONS": "practice_direction",
    "PRACTICE DIRECTION": "practice_direction",
    "INSTRUCTIONS DU PRÉSIDENT DE LA COUR SUPRÊME": "practice_direction",
    "INSTRUCTION DU PRÉSIDENT DE LA COUR SUPRÊME": "practice_direction",
    "DIRECTIVES PRATIQUES": "practice_direction",
    "DIRECTIVE PRATIQUE": "practice_direction",
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
# Surer signs that a range holds an instrument's body, not only its title over a contents list: its
# enacting words ("HEREBY ORDERS:", "ATEGETSE:", "ARRÊTE :") and its signature's place and date
# ("Kigali, on 22/03/2016"). PREAMBLE alone missed "The Prime Minister," ended by a comma (2026-10-29).
BODY = re.compile(
    r"^(?:HEREBY\s+)?(?:ORDERS|ADOPTS|ENACTS|DECREES|PROMULGATES|DETERMINES)\s*:\s*$"
    r"|^(?:ATEGETSE|YEMEJE|ITANGAJE|ARR[ÊE]TE|ADOPTE|D[ÉE]CR[ÈE]TE|PROMULGUE)\s*:\s*$"
    r"|^(?:Kigali|Bikorewe\s+i\s+Kigali|Fait\s+[àa]\s+Kigali|Done\s+(?:in|at)\s+Kigali)\s*,?\s+(?:on|ku\s?wa|le)\b",
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
    # A page read as one column of no language whose next page prints the three languages side by side
    # (an RLRC file's cover, a Gazette's front page): its title lines run across the page, so no gutter
    # was measured. It is read again by the next page's columns, each line across them kept whole in
    # its own language (before 2026-10-29 such a page reached no document).
    for k, split in enumerate(out):
        nxt = out[k + 1] if k + 1 < len(out) else None
        if (
            nxt is not None
            and split["layout"] == "single"
            and all(c["language"] is None for c in split["columns"])
            and nxt["layout"] == "parallel"
            and not (tables or {}).get(split["page"])
        ):
            out[k] = cover_page(pages[k], split, [(c["x0"], c["x1"]) for c in nxt["columns"]])
    return out


def _rows(words: list[dict]) -> list[list[dict]]:
    """Words in printed rows by their vertical centre (as lines_of), each row left to right."""
    if not words:
        return []
    height = statistics.median(w["ymax"] - w["ymin"] for w in words) or 1.0
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["ymin"] + w["ymax"]) / 2):
        centre = (word["ymin"] + word["ymax"]) / 2
        if (
            rows
            and abs(centre - statistics.mean((w["ymin"] + w["ymax"]) / 2 for w in rows[-1])) <= 0.45 * height
        ):
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: w["xmin"]) for row in rows]


def _line(words: list[dict], **more) -> dict:
    return {
        "text": " ".join(w["text"] for w in words),
        "x0": round(min(w["xmin"] for w in words), 1),
        "y0": round(min(w["ymin"] for w in words), 1),
        "x1": round(max(w["xmax"] for w in words), 1),
        "y1": round(max(w["ymax"] for w in words), 1),
        **more,
    }


def line_language(text: str) -> str | None:
    """A single line's language by its marker words (gazette_layout.MARKERS), where one language has
    more of them than the others; None where none or a tie."""
    counts = gazette_layout.language([text])[1]
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return ranked[0][0] if len(ranked) == 1 or ranked[0][1] > ranked[1][1] else None


def cover_page(page: dict, split: dict, spans: list[tuple[float, float]]) -> dict:
    """A cover or front page read by the next page's columns (split_issue): a row whose words cross a
    gutter is one line across the page, given to the column of its own language (line_language), or to
    every column where its language cannot be told (a date, a number); any other row's words go to the
    columns they lie in. Each line is marked "cover"; the page keeps its furniture."""
    gutters = [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)]
    columns: list[list[dict]] = [[] for _ in spans]
    furniture_ids = {(line["y0"], line["text"]) for line in split["furniture"]}

    def nearest(word: dict) -> int:
        centre = (word["xmin"] + word["xmax"]) / 2
        distance = [0 if a <= centre < b else min(abs(centre - a), abs(centre - b)) for a, b in spans]
        return distance.index(min(distance))

    rows = [
        row for row in _rows(page["words"]) if (_line(row)["y0"], _line(row)["text"]) not in furniture_ids
    ]
    across = [any(w["xmin"] < g1 and w["xmax"] > g0 for w in row for g0, g1 in gutters) for row in rows]
    # A run of exactly as many lines across the page as there are languages is the title in each, printed
    # in the columns' order (Kinyarwanda, English, French), whatever their words.
    by_order: dict[int, int] = {}
    k = 0
    while k < len(rows):
        if not across[k]:
            k += 1
            continue
        run = k
        while k < len(rows) and across[k]:
            k += 1
        if k - run == len(spans):
            by_order.update({row: n for n, row in enumerate(range(run, k))})
    for k, row in enumerate(rows):
        whole = _line(row)
        if across[k]:
            language = line_language(whole["text"])
            if k in by_order:
                targets = [by_order[k]]
            elif language in LANGUAGES[: len(spans)]:
                targets = [LANGUAGES.index(language)]
            else:
                targets = range(len(spans))
            for target in targets:
                columns[target].append({**whole, "cover": True, "across": True})
            continue
        parts: dict[int, list[dict]] = {}
        for word in row:
            parts.setdefault(nearest(word), []).append(word)
        for index, words in parts.items():
            columns[index].append(_line(words, cover=True))
    return {
        **split,
        "layout": "cover",
        "columns": [
            {
                "language": LANGUAGES[i] if i < len(LANGUAGES) else None,
                "x0": round(a, 1),
                "x1": round(b, 1),
                "lines": sorted(lines, key=lambda line: line["y0"]),
            }
            for i, ((a, b), lines) in enumerate(zip(spans, columns, strict=True))
        ],
    }


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


# A Gazette also prints notices that are not legislation (owner, 2026-10-29: recorded as notices, not
# prepared for review): changes of name, the statutes of associations, churches and cooperatives, the
# Rwanda Development Board's journal of companies and industrial property, reference land prices, and
# court decisions. Each by the heading that opens it, in any of the three languages.
NOTICE_HEADINGS = (
    (
        "name_change",
        re.compile(
            r"(?i)^(?:icyemezo|itangazo)?\s*(?:cyo|ryo)?\s*guhindura\s+amazina\b|change\s+of\s+names?\b|changement\s+de\s+noms?\b"
        ),
    ),
    (
        "statutes",
        re.compile(
            r"(?i)^amategeko\s*[-–]?\s*shingiro\b|^statuts\b|^statutes\b"
            r"|^(?:constitution|statuts)\s+(?:of|de|du|des)\s+(?!the\s+republic|la\s+r[ée]publique)"
        ),
    ),
    ("cooperative", re.compile(r"(?i)^(?:[a-d]\.\s*)?(?:amakoperative|cooperatives?|coop[ée]ratives?)\b")),
    (
        "companies_and_industrial_property",
        re.compile(
            r"(?i)industrial\s+property|propri[ée]t[ée]\s+industrielle|journal\s+of\s+(?:the\s+)?rdb|\brdb\s+journal"
        ),
    ),
    (
        "land_prices",
        re.compile(r"(?i)ibiciro\s+fatizo|reference\s+(?:land\s+)?prices?|prix\s+de\s+r[ée]f[ée]rence"),
    ),
    ("court_decision", re.compile(r"(?i)^urubanza\b|\bRS/INCONST\b|^(?:judgment|arr[êe]t)\s+(?:n|rs)\b")),
    # The issue's own front matter: its contents ("Ibirimo/Summary/Sommaire").
    ("front_matter", re.compile(r"(?i)^(?:ibirimo|summary|sommaire)\b")),
)
# The same, from the issue's name as the Gazette lists it ("… _Amazina + RDB_Journal").
NOTICE_ISSUE = (
    ("name_change", re.compile(r"(?i)amazina|names")),
    ("companies_and_industrial_property", re.compile(r"(?i)\brdb\b|industrial[\s_]prop|journal")),
    ("cooperative", re.compile(r"(?i)cooperative|amakoperative")),
    ("land_prices", re.compile(r"(?i)ibiciro|fatizo")),
    ("court_decision", re.compile(r"(?i)urubanza|imanza")),
    ("statutes", re.compile(r"(?i)\bfbo\b|imiryango|amatorero|abahesha")),
)


def notice_kind(text: str) -> str | None:
    for kind, pattern in NOTICE_HEADINGS:
        if pattern.search(text.strip()):
            return kind
    return None


def notices(
    issue_streams: dict[str, list[dict]],
    found: list[dict],
    issue_name: str = "",
    front_pages: set[int] | frozenset = frozenset(),
) -> list[dict]:
    """The notices of an issue (NOTICE_HEADINGS): in each language, the lines outside every instrument
    from a notice's heading to the next instrument or notice; in an issue with no instrument at all
    whose name or lines say it holds notices, all its lines. Each {kind, language, start, end, pages}."""
    out = []
    for language, lines in issue_streams.items():
        spans = sorted(
            (v["start"], v["end"])
            for entry in found
            for lang, v in entry["versions"].items()
            if lang == language
        )
        inside = [False] * len(lines)
        for start, end in spans:
            for k in range(start, min(end, len(lines))):
                inside[k] = True
        current = None
        for k, line in enumerate(lines):
            if inside[k]:
                if current:
                    out.append(current)
                    current = None
                continue
            kind = notice_kind(line["text"]) or ("front_matter" if line["page"] in front_pages else None)
            if kind and (current is None or kind != current["kind"]):
                if current:
                    out.append(current)
                current = {
                    "kind": kind,
                    "language": language,
                    "start": k,
                    "end": k + 1,
                    "pages": [line["page"]] * 2,
                }
            elif current:
                current["end"] = k + 1
                current["pages"][1] = line["page"]
        if current:
            out.append(current)
        if not found and lines and not any(n["language"] == language for n in out):
            kind = next((kind for kind, pattern in NOTICE_ISSUE if pattern.search(issue_name)), None)
            if kind:
                out.append(
                    {
                        "kind": kind,
                        "language": language,
                        "start": 0,
                        "end": len(lines),
                        "pages": [lines[0]["page"], lines[-1]["page"]],
                    }
                )
    return out


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
        annex["title"] = shortened(" ".join(line["text"] for line in first[:6] if line["text"]), 300)
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


def shortened(text: str, limit: int) -> str:
    """Text cut to at most `limit` characters at a word, marked "…" where it was cut."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if not text[limit - 1].isspace():  # cut inside a word: back to the word before it
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


def openings(lines: list[dict], language: str) -> list[dict]:
    """Where each instrument begins in one language: its heading, kind, number, date and title."""
    found = []
    i = 0
    while i < len(lines):
        text = lines[i]["text"].strip()
        joined = 1
        match = OPENING[language].match(text)
        constitution = CONSTITUTION[language].match(text)
        if not (match or constitution) and (
            re.search(r"\bN\s?[°ºO0\uf0b0]\.?$", text)  # the number on the next line
            or KIND_ALONE[language].match(text)  # the kind alone, its number or date on the next line
            or CONSTITUTION_BEGUN[language].match(text)
            or KIND_HEAD.match(text)  # the kind's words begun, going on over the next line or two
        ):
            for more in (2, 3):
                if i + more > len(lines) or any(
                    line.get("page") != lines[i].get("page") for line in lines[i : i + more]
                ):
                    break
                text = " ".join(line["text"].strip() for line in lines[i : i + more])
                match = OPENING[language].match(text)
                constitution = CONSTITUTION[language].match(text)
                if match or constitution:
                    joined = more
                    break
        if not (match or constitution):
            i += 1
            continue
        heading = [line["text"].strip() for line in lines[i : i + joined]]
        j = i + joined
        # The title runs on in capitals until the contents, the first non-capital line or a division or
        # article heading, and never past HEADING_MAX: a ratification law's title in a narrow column
        # takes a dozen lines, so no count of lines bounds it. A title begun at the foot of a page goes
        # on over the next page's running head, which the Gazette sometimes misspells ("Oficcial
        # Gazette nº 53 of 31/12/2012"), so its date is the instrument's, never the issue's.
        while j < len(lines) and len(" ".join(heading)) < HEADING_MAX:
            text = lines[j]["text"].strip()
            if lines[j].get("page") != lines[j - 1].get("page") and RUNNING_HEAD.search(text):
                j += 1
                continue
            letters = [c for c in text if c.isalpha()]
            if (
                CONTENTS.match(text)
                or CONTENTS_WORDS.match(text)
                or not letters
                or sum(c.isupper() for c in letters) < 0.8 * len(letters)
                or STANDARD_ARTICLE[language].match(text)
                or any(pattern.match(text) for _, pattern in DIVISIONS[language])
            ):
                break
            heading.append(text)
            j += 1
        title = " ".join(heading)
        # Its date is the first printed in its opening lines, never one a long title goes on to cite
        # (the date of an agreement it ratifies).
        opening = " ".join(heading[:8])
        word = match.group(1).replace("'", "’").upper() if match else ""
        word = (
            word.replace("ARRETE", "ARRÊTÉ")
            .replace("PRESIDENTIEL", "PRÉSIDENTIEL")
            .replace("MINISTERIEL", "MINISTÉRIEL")
            .replace("REGLEMENT", "RÈGLEMENT")
            .replace("DECRET", "DÉCRET")
            .replace("LEGISLATIVE", "LÉGISLATIVE")
            .replace("PRESIDENT DE", "PRÉSIDENT DE")
            .replace("SUPREME", "SUPRÊME")
            .replace("SUPRÊME COURT", "SUPREME COURT")
        )
        date = _plain_date(opening) or spoken_date(opening)
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
                PREAMBLE.match(line["text"].strip()) or BODY.match(line["text"].strip())
                for line in lines[opening["start"] : end]
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
                # The title printed again (over the body, after the contents): the same instrument, whose
                # body is under this second title. Before 2026-10-29 the body was not noted, so a run of
                # orders with one title and date ("… APPOINTING A DIRECTOR") was taken for one law printed
                # with several numbers and merged (_same_title_other_number): Gazette n° 14 of 04/04/2016
                # gave 9 of its 22 orders.
                entry["versions"][language]["end"] = end
                entry["versions"][language]["pages"][1] = lines[end - 1]["page"]
                entry["versions"][language]["has_body"] = (
                    entry["versions"][language]["has_body"] or opening["has_body"]
                )
                continue
            entry["versions"][language] = {
                "title": opening["title"],
                "has_body": opening["has_body"],
                "start": opening["start"],
                "end": end,
                "pages": [lines[opening["start"]]["page"], lines[end - 1]["page"]],
            }
    found = [by_number[key] for key in order]
    found = _join_dated(found)
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


def _pages_overlap(a: list[int], b: list[int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)


def _join_dated(found: list[dict]) -> list[dict]:
    """An instrument one language printed without its number (known there by its date only) and the
    same instrument in other languages by its number: one instrument, when the date is the same, their
    languages differ and their pages overlap (2026-10-29: Practice Directions n° 002/2025 became two)."""
    out = []
    for entry in found:
        if entry["number"] != entry.get("date"):
            out.append(entry)
            continue
        pages = [
            min(v["pages"][0] for v in entry["versions"].values()),
            max(v["pages"][1] for v in entry["versions"].values()),
        ]
        twin = next(
            (
                other
                for other in found
                if other is not entry
                and other["number"] != other.get("date")
                and other.get("date") == entry["date"]
                and not set(other["versions"]) & set(entry["versions"])
                and any(_pages_overlap(v["pages"], pages) for v in other["versions"].values())
            ),
            None,
        )
        if twin is None:
            out.append(entry)
        else:
            twin["versions"].update(entry["versions"])
    return out


def fill_by_page(issue_streams: dict[str, list[dict]], found: list[dict], min_words: int = 20) -> None:
    """A language printed beside an instrument whose heading in that language was not recognised: the
    run of its lines in no instrument, on the pages the instrument's other languages share, is that
    language's version, marked found by its pages ("by_page") for the reviewer to check. The languages
    of the Gazette are printed side by side, so their pages are the same."""
    for language, lines in issue_streams.items():
        taken = [False] * len(lines)
        for entry in found:
            version = entry["versions"].get(language)
            if version:
                for k in range(version["start"], min(version["end"], len(lines))):
                    taken[k] = True
        k = 0
        while k < len(lines):
            if taken[k]:
                k += 1
                continue
            start = k
            while k < len(lines) and not taken[k]:
                k += 1
            run = lines[start:k]
            if sum(len(line["text"].split()) for line in run) < min_words:
                continue
            pages = [run[0]["page"], run[-1]["page"]]
            candidates = [
                (sum(_pages_overlap(v["pages"], pages) for v in entry["versions"].values()), n)
                for n, entry in enumerate(found)
                if language not in entry["versions"] and entry["versions"]
            ]
            best = max(candidates, default=(0, None))
            if best[0] <= 0:
                continue
            heading = [line["text"].strip() for line in run[:6] if _capitals(line["text"])][:4]
            found[best[1]]["versions"][language] = {
                "title": " ".join(heading)
                or found[best[1]]["versions"][next(iter(found[best[1]]["versions"]))]["title"],
                "has_body": True,
                "start": start,
                "end": k,
                "pages": pages,
                "by_page": True,
            }


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
    cover: list[dict] = []  # the blocks of a cover opening the version, one of which is its title
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
        if line.get("cover"):
            # A cover (an RLRC file's: the title in each language, the languages it was translated
            # from, where it was adopted, the order ratifying it, its Gazette) in its blocks as printed:
            # one block is the title (cover_title), every other a paragraph. It never runs on into one
            # heading, nor into the title printed again on the next page.
            last = nodes[-1] if nodes else None
            joins = (
                last is not None
                and previous is not None
                and previous.get("cover")
                and last.get("lines", [None])[-1] is previous
                and previous["page"] == line["page"]
                and previous.get("across") == line.get("across")
                and line["y0"] - previous["y1"] < 0.7 * (line["y1"] - line["y0"])
            )
            if joins:
                last["text"] += " " + text
                last["lines"].append(line)
                last.setdefault("differences", []).extend(line.get("differences", []))
            else:
                nodes.append(
                    {
                        "kind": "paragraph",
                        "level": "paragraph",
                        "text": text,
                        "page": line["page"],
                        "lines": [line],
                        "differences": list(line.get("differences", [])),
                    }
                )
                if part == "heading" and not any(n["kind"] == "heading" for n in nodes):
                    cover.append(nodes[-1])
            previous = line
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
    title = cover_title(cover, language)
    if title is not None:
        title["kind"], title["level"] = "heading", "heading-1"
    return nodes


def cover_title(blocks: list[dict], language: str) -> dict | None:
    """A cover's title among its blocks: the first in capitals in the version's own language, else the
    first in capitals whose language is not told (Kinyarwanda's marker words are rarer in a title), else
    the first in capitals. A trilingual cover gives each version every language's title."""
    capitals = [block for block in blocks if _capitals(block["text"])]
    told = [line_language(block["text"]) for block in capitals]
    return next(
        (b for b, lang in zip(capitals, told, strict=True) if lang == language),
        next(
            (b for b, lang in zip(capitals, told, strict=True) if lang is None),
            capitals[0] if capitals else None,
        ),
    )


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
    sentence = re.sub(
        r"\b(rwanda|republic|république|repubulika)\b", lambda m: m.group(1).capitalize(), sentence
    )
    # An acronym printed in brackets keeps its capitals: "(AMA)", "(RURA)", "(EAC)".
    acronyms = {m.group(1).lower(): m.group(1) for m in re.finditer(r"\(([A-Z]{2,8})\)", title)}
    return re.sub(r"\(([a-z]{2,8})\)", lambda m: f"({acronyms.get(m.group(1), m.group(1))})", sentence)


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
# The date printed above the signatures, in each language and as typeset: "Kigali, on 22/01/2007",
# "Kigali, le 13/04/2018", "Kigali, kuwa 26/04/2007", "Kigali ku wa 27/02/2015", "Kigali on 30/03/2009",
# "Kigali, on27/7/2015". Without its comma it opens its line, so a preamble's "signed in Kigali on
# 12/05/2010" is never taken for it (2026-10-30: only "Kigali, on …" was read, 1,032 works unsigned).
KIGALI = re.compile(
    r"(?:Kigali\s*,\s*|^\s*Kigali\s+(?=on|le|ku))(?:on|le|kuwa|ku\s+wa)?\s*"
    r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*((?:19|20)\d\d)",
    re.I | re.M,
)
# Who adopted it, as the enacting words print it: "THE PARLIAMENT HAS ADOPTED AND WE SANCTION, PROMULGATE
# …", "THE TRANSITIONAL NATIONAL ASSEMBLY HAD ADOPTED AND WE SANCTION …" (in capitals, as printed).
ADOPTED_BY = re.compile(r"\b(?:THE\s+)?([A-Z]+(?:\s+[A-Z]+){0,4})\s+HA[SD]\s+ADOPTED\b")
# When a text commences, in each language (2026-10-30: only "… on the date of its publication" was read):
# "comes into force on the day of its publication", "entre en vigueur le jour de sa publication", "Iri
# teka ritangira gukurikizwa umunsi ryatangarijweho mu Igazeti"; or of its signature, "… ku munsi
# rishyiriweho umukono".
IN_FORCE_ON_PUBLICATION = re.compile(
    r"(?:comes|shall come|enters|shall enter) into force on the (?:date|day) of its publication"
    r"|entre(?:ra)? en vigueur (?:le jour|[àa] la date|au jour) de sa publication"
    r"|gukurikizwa\s+(?:ku\s+)?u?munsi\s+r[iy]a?tangarij[wy]e(?:ho)?",
    re.I,
)
IN_FORCE_ON_SIGNATURE = re.compile(
    r"(?:comes|shall come|enters|shall enter) into force on the (?:date|day) of its signature"
    r"|entre(?:ra)? en vigueur (?:le jour|[àa] la date|au jour) de sa signature"
    r"|gukurikizwa\s+(?:ku\s+)?u?munsi\s+r[iy]a?shyiriweho\s+umukono",
    re.I,
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


# The Cabinet's step, as each language prints it: "After consideration and approval by the Cabinet, in
# its session of 09/09/2015", "… by Cabinet, meeting in its session of 12 November 2003", "Après examen
# et adoption par le Conseil des Ministres, en sa séance du 14/09/2018", "Amaze kubona icyemezo cy'Inama
# y'Abaminisitiri yo kuwa 20 Werurwe 2002" (2026-10-30: only the first, in English, was read).
CABINET = re.compile(
    r"(?:approval by (?:the )?Cabinet,?\s+(?:meeting\s+)?in its (?:session|meeting|sitting) of"
    r"|adoption par le Conseil des Ministres,?\s+en sa s[ée]ance du"
    r"|Inama\s+y.\s?Abaminisitiri,?\s+yo\s+ku\s*wa)"
    r"\s+((\d{1,2})\s*/\s*(\d{1,2})\s*/\s*((?:19|20)\d\d)|\d{1,2}\s+\w+\s+(?:19|20)\d\d)",
    re.I,
)
CABINET_WORD = re.compile(r"\bCabinet\b|Conseil des Ministres|Abaminisitiri", re.I)


def cabinet_date(found: re.Match) -> str | None:
    if found.group(2):
        return f"{found.group(4)}-{int(found.group(3)):02d}-{int(found.group(2)):02d}"
    return spoken_date(found.group(1))


# An RLRC cover's facts, as its English prints them.
COVER_ADOPTED = re.compile(r"Adopted (?:in|at) ([A-Z][\w’' -]+?),?\s+on\s+(.{4,40}?\d{4})", re.I)
COVER_RATIFIED = re.compile(
    r"Ratified by (?:the )?(Presidential Order|Prime Minister[’']s Order|Law|Ministerial Order)\s+n\s?[°ºo]\.?\s*(\S+)\s+of\s+(.{4,40}?\d{4})",
    re.I,
)
COVER_GAZETTE = re.compile(
    r"Official Gazette of the Republic of Rwanda\s+n\s?[°ºo]\.?\s*(special|\d+(?:\s*bis|\s*ter)?)\s+of\s+(.{4,40}?\d{4})",
    re.I,
)
COVER_TRANSLATED = re.compile(
    r"Translated into Kinyarwanda from (French|English)(?:\s+and\s+(French|English))?", re.I
)


def _ordinal_free(text: str) -> str:
    """'12 th June 2019' and '6th November 2019' as '12 June 2019'."""
    return re.sub(r"(\d)\s*(?:st|nd|rd|th)\b", r"\1", text)


def signers(lines: list[dict]) -> list[dict]:
    """Everyone who signed, in printed order, from the block after "Kigali, on …": each {name, role,
    page}. Older laws print the office before the name, newer ones after."""
    start = next((i for i, line in enumerate(lines) if KIGALI.search(line["text"])), None)
    if start is None:
        return []
    block = [
        line
        for line in lines[start + 1 :]
        if line["text"].strip()
        and not SIGNED.match(line["text"].strip())
        and not FURNITURE.match(line["text"].strip())
        and not re.match(
            r"^(Seen and sealed|Bibonywe kandi|Vu et scell)|^Republic:$", line["text"].strip(), re.I
        )
    ]
    texts = [line["text"].strip() for line in block]
    first_role = next((i for i, t in enumerate(texts) if ANY_ROLE.match(t)), None)
    if first_role is None:
        return []
    name_first = any(NAME.search(t) for t in texts[:first_role])
    out: list[dict] = []
    for i, text in enumerate(texts):
        if not ANY_ROLE.match(text):
            continue
        step_by = -1 if name_first else 1
        j = i + step_by
        while 0 <= j < len(texts) and not NAME.search(texts[j]) and abs(j - i) <= 3:
            j += step_by
        if 0 <= j < len(texts) and NAME.search(texts[j]):
            person = {
                "name": texts[j],
                "role": re.sub(r"^The\s+", "", text, flags=re.I),
                "page": block[i].get("page"),
            }
            if not any(p["name"] == person["name"] for p in out):
                out.append(person)
    return out


def numbers_agree(first, second) -> bool:
    """'055/2024' printed and '055' in a file's name are one number; so are '01' and '1'."""

    def plain(value) -> str:
        # A file's name writes "57-2008" and "002-16.10-TC" for the printed "57/2008" and "002/16.10/TC".
        text = re.sub(r"\s+", "", str(value or "")).casefold().replace("°", "").replace("º", "")
        return re.sub(r"[-.]", "/", text)

    a, b = plain(first), plain(second)
    return a == b or (a.split("/")[0].lstrip("0") or "0") == (b.split("/")[0].lstrip("0") or "0")


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


# The record's family for each kind the reader names (schema.record.FAMILIES); the orders and decrees are
# executive instruments. Before 2026-10-29 every kind but a law was filed as one.
FAMILY = {
    "constitution": "constitution",
    "law": "legislation",
    "organic_law": "legislation",
    "decree_law": "legislation",
    "ordinance": "legislation",
    "regulations": "subsidiary_legislation",
    "practice_direction": "subsidiary_legislation",
    "agreement": "treaty",
    "treaty": "treaty",
    "convention": "treaty",
    "protocol": "treaty",
    "charter": "treaty",
    "other": "official_document",
}


# The word a printed title opens with, in each language: where a file's first heading also carries a
# masthead, a logo's words, the band above it or the other languages' titles ("N° 16 15 REPUBLIQUE
# RWANDAISE A. — LOIS. … LOI N° 35/90 DU 22 JUIN 1990 PORTANT …", "Kingdom of the Netherlands AIR
# SERVICES AGREEMENT BETWEEN …", "AMASEZERANO … CONVENTION ON PSYCHOTROPIC SUBSTANCES CONVENTION SUR …").
TITLE_WORDS = {
    "en": r"LAW|ORDINANCE|DECREE|ORDER|CONVENTION|AGREEMENT|PROTOCOL|TREATY|CHARTER|STATUTES|MEMORANDUM|"
    r"INSTRUCTIONS?|REGULATIONS?",
    "fr": r"LOI|ORDONNANCE|D[ÉE]CRET|ARR[ÊE]T[ÉE]|CONVENTION|ACCORD|PROTOCOLE|TRAIT[ÉE]|CHARTE|STATUTS|"
    r"R[ÈE]GLEMENT|INSTRUCTIONS?",
    "rw": r"ITEGEKO|ITEKA|AMASEZERANO|AMABWIRIZA|AMATEKA",
}
TITLE_WORD = {
    language: re.compile(rf"(?:{words})", re.I)
    for language, words in {**TITLE_WORDS, "any": "|".join(TITLE_WORDS.values())}.items()
}


def _capital_word(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return len(letters) <= 1 or sum(c.isupper() for c in letters) >= 0.8 * len(letters)


def _kind_word(word: str, language: str = "any") -> bool:
    return bool(TITLE_WORD[language].fullmatch(word.strip(".,:;—-'’")))


def title_from_heading(text: str, language: str = "any") -> str | None:
    """A title from a first heading: from the word it opens with in this language (and, where no other
    title comes before it, the capitals before that word, as "AIR SERVICES AGREEMENT"), to where it
    ends: the capitals running out, a full stop, or its kind word printed again in another language."""
    words = " ".join(text.split()).split(" ")
    start = next((k for k, w in enumerate(words) if _kind_word(w, language)), None)
    if start is None and language != "any":
        start = next((k for k, w in enumerate(words) if _kind_word(w)), None)
    if start is None:
        return None
    kind = words[start].strip(".,:;—-'’").upper()
    capitals = _capital_word(words[start])
    if capitals and not any(_kind_word(w) for w in words[:start]):
        while (
            start > 0
            and _capital_word(words[start - 1])
            and any(c.isalpha() for c in words[start - 1])
            and not words[start - 1].endswith((".", "—", ":"))
        ):
            start -= 1
    out: list[str] = []
    for word in words[start:]:
        if out and capitals and not _capital_word(word):
            break
        if len(out) > 2 and word.strip(".,:;—-'’").upper() == kind:
            break
        out.append(word)
        # A full stop ends it, but not a number's ("1.", "n°.") unless it is the year's.
        if (
            word.endswith(".")
            and len(out) > 3
            and not re.fullmatch(r"(?:\d{1,3}|n[°o]?|art|no)\.", word, re.I)
        ):
            break
    title = " ".join(out).strip(" .,;:—-")
    return title if len(title) >= 12 else None


def printed_title(versions: dict[str, list[dict]]) -> tuple[str, int | None] | None:
    """A text's own title, where its heading was not recognised (an RLRC copy of an older text, titled
    from the file's name until 2026-10-30): its cover's title in English, else French, else
    Kinyarwanda; else its first heading's, as title_from_heading reads it. With its page."""
    for language in ("en", "fr", "rw"):
        cover = [line for line in versions.get(language, []) if line.get("cover")]
        title = (
            next((n for n in structure(cover, language) if n["kind"] == "heading"), None) if cover else None
        )
        if title:
            return title["text"], title["page"]
    for language in ("en", "fr", "rw"):
        lines = [line for line in versions.get(language, [])[:80] if not line.get("cover")]
        first = (
            next((n for n in structure(lines, language) if n["kind"] == "heading"), None) if lines else None
        )
        found = title_from_heading(first["text"], language) if first else None
        if found:
            return found, first["page"]
    return None


# Who issues an order, as its opening names the office: "The Minister of Health,", "The Prime Minister,",
# "We, KAGAME Paul, President of the Republic;", "Le Ministre de la Justice,", "Nous, HABYARIMANA
# Juvénal, Président de la République,", "Minisitiri w'Ubuzima,", "Le Gouverneur Général," (2026-10-30:
# read only from the signatures, which 2,572 works' readers did not find).
ISSUER = {
    "en": re.compile(
        r"^(?:We,\s+[^,;]{3,60},\s+)?(?:The\s+)?(President of the Republic|Prime Minister|"
        r"Minister\s+(?:of|for|in)\s+[^,;:]{3,120}|Governor[- ]General)\s*[,;:]",
        re.I,
    ),
    "fr": re.compile(
        r"^(?:Nous,\s+[^,;]{3,60},\s+)?(?:Le\s+|La\s+)?(Pr[ée]sident de la R[ée]publique|Premier Ministre|"
        r"Ministre\s+(?:de|des|du|d[’'])\s*[^,;:]{3,120}|(?:Vice-)?Gouverneur[- ]G[ée]n[ée]ral)\s*[,;:]",
        re.I,
    ),
    "rw": re.compile(
        r"^(?:Twebwe,\s+[^,;]{3,60},\s+)?(Perezida wa Repubulika|Minisitiri w.\s?Intebe|"
        r"Minisitiri\s+w.\s?[^,;:]{3,120})\s*[,;:]",
        re.I,
    ),
}


def issuer(versions: dict[str, list[dict]]) -> dict | None:
    """The office an order's opening names, in English, else French, else Kinyarwanda: {value, page,
    words}, from the first paragraphs before its articles."""
    for language in ("en", "fr", "rw"):
        pattern = ISSUER[language]
        for node in versions_nodes(versions.get(language, [])[:120], language)[:25]:
            if node["kind"] != "paragraph":
                continue
            found = pattern.match(" ".join(node["text"].split()))
            if found:
                return {"value": found.group(1).strip(), "page": node["page"], "words": found.group(0)}
    return None


def suggest(versions: dict[str, list[dict]], entry: dict, issue: dict) -> tuple[dict, dict, dict, dict]:
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
    # Where it states when it commences: in English, else in French, else in Kinyarwanda (a text
    # printed in one or two of them).
    worded = paragraphs + [
        n
        for language in ("fr", "rw")
        if language in versions
        for n in versions_nodes(versions[language], language)
        if n["kind"] == "paragraph"
    ]
    values: dict = {
        "identity.type": entry["kind"],
        "identity.family": FAMILY.get(entry["kind"], "executive_instrument"),
    }
    # The words and page each value is read from, shown beside it to the reviewer (2026-10-29: every
    # value; before, only the legal facts carried theirs).
    evidence: dict = {}

    def said(key: str, value, page, words: str) -> None:
        evidence[key] = {"value": value, "page": page, "evidence": " ".join(str(words).split())[:300]}

    heading = entry["versions"].get("en", next(iter(entry["versions"].values())))
    title = heading["title"]
    heading_page = (heading.get("pages") or [None])[0]
    cover = [line for line in versions.get("en", []) if line.get("cover")]
    if entry.get("title_from_listing"):
        # The text's own title as printed, before the file's name: its cover's, else its first heading's.
        found = printed_title(versions)
        if found:
            title, heading_page = found
    if title:
        values["identity.full_title"] = values["identity.title"] = sentence_case(title)
        values["identity.citation"] = values["identity.full_title"]
        for key in ("identity.title", "identity.full_title", "identity.citation"):
            said(key, values[key], heading_page, title)
    if entry["number"] != "constitution" and not entry.get("number_made"):
        values["identity.number.value"] = entry["number"]
        said("identity.number.value", entry["number"], heading_page, title)
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
        said("identity.year", values["identity.year"], heading_page, title)
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
    every = [line for language in ("en", "fr", "rw") for line in versions.get(language, [])]
    family = values["identity.family"]
    order = family == "executive_instrument" and entry["kind"] not in ("instructions",)
    by_events = family in ("subsidiary_legislation",) or entry["kind"] == "instructions"
    signed = next((line for line in every if KIGALI.search(line["text"])), None)
    if signed:
        found = KIGALI.search(signed["text"])
        when = f"{found.group(3)}-{int(found.group(2)):02d}-{int(found.group(1)):02d}"
        # The one who signs first signs under the date: a law is promulgated, an order signed, an
        # instruction or regulation signed or made (2026-10-29: an order's date was filed as promulgated).
        key = (
            "event:signed.date"
            if entry["kind"] == "instructions"
            else "event:made.date"
            if by_events
            else "step:signed.date"
            if order
            else "step:promulgated.date"
        )
        values[key] = when
        said(key, when, signed.get("page"), found.group(0))
        kigali = {"value": when, "page": signed.get("page"), "evidence": found.group(0)}
        if not by_events:
            for step in ("countersigned", "sealed"):  # the other signatures carry no date of their own
                inferred[f"step:{step}.date"] = dict(kigali)
    # The Cabinet's approval: in English, else in French, else in Kinyarwanda.
    cabinet_found, cabinet = None, None
    for language in ("en", "fr", "rw"):
        joined = " ".join(" ".join(line["text"].split()) for line in versions.get(language, []))
        cabinet_found = CABINET.search(joined)
        if cabinet_found:
            cabinet = next((line for line in versions[language] if CABINET_WORD.search(line["text"])), None)
            break
    when = cabinet_date(cabinet_found) if cabinet_found and order else None
    if when:
        values["step:approved:cabinet.date"] = when
        said("step:approved:cabinet.date", when, (cabinet or {}).get("page"), cabinet_found.group(0))
    people = signers(versions.get("en", [])) or next(
        (found for found in (signers(versions.get(k, [])) for k in versions) if found), []
    )
    if order or by_events:
        if people:
            first = people[0]
            values["profile.signed_by"] = first["name"]
            said("profile.signed_by", first["name"], first["page"], f"{first['name']}, {first['role']}")
            authority = (
                "profile.maker" if by_events and entry["kind"] != "instructions" else "profile.authority"
            )
            values[authority] = first["role"]
            values["issued_by"] = first["role"]
            said(authority, first["role"], first["page"], first["role"])
            said("issued_by", first["role"], first["page"], first["role"])
            if order:
                values["step:signed.person"] = f"{first['name']}, {first['role']}"
                justice = [p for p in people[1:] if re.match(r"Minister of Justice", p["role"], re.I)]
                others = [p for p in people[1:] if p not in justice]
                if others:
                    values["profile.countersigned_by"] = [f"{p['name']}, {p['role']}" for p in others]
                    values["step:countersigned.person"] = values["profile.countersigned_by"][0]
                if justice:
                    values["step:sealed.person"] = f"{justice[0]['name']}, {justice[0]['role']}"
    else:
        for language in ("en", *[k for k in versions if k != "en"]):
            for step, person in signatories([line["text"] for line in versions.get(language, [])]).items():
                values.setdefault(f"step:{step}.person", person)
    if "issued_by" not in values and entry["kind"] not in ("law", "organic_law", "constitution"):
        # No signatures read: the office its opening names, as printed.
        opening = issuer(versions)
        if opening:
            values["issued_by"] = opening["value"]
            said("issued_by", opening["value"], opening["page"], opening["words"])
            if order or by_events:
                authority = (
                    "profile.maker" if by_events and entry["kind"] != "instructions" else "profile.authority"
                )
                values.setdefault(authority, opening["value"])
                said(authority, values[authority], opening["page"], opening["words"])
    # An RLRC cover's own facts: where and when it was adopted, the order ratifying it, its Gazette, and
    # which of its languages is a translation.
    cover_text = " ".join(" ".join(line["text"].split()) for line in cover)
    cover_page = cover[0].get("page") if cover else None
    adopted_cover = COVER_ADOPTED.search(cover_text)
    if adopted_cover:
        place, when = adopted_cover.group(1).strip(), spoken_date(_ordinal_free(adopted_cover.group(2)))
        values["profile.adopted_at"] = place
        said("profile.adopted_at", place, cover_page, adopted_cover.group(0))
        if when:
            values["event:adopted.date"] = when
            said("event:adopted.date", when, cover_page, adopted_cover.group(0))
            values.setdefault("identity.year", when[:4])
            said("identity.year", values["identity.year"], cover_page, adopted_cover.group(0))
    ratified = COVER_RATIFIED.search(cover_text)
    if ratified and family == "treaty":
        when = spoken_date(_ordinal_free(ratified.group(3)))
        row = {"party": "Rwanda", **({"ratified": when} if when else {})}
        values["profile.parties"] = [row]
        said("profile.parties", [row], cover_page, ratified.group(0))
    gazette_cover = COVER_GAZETTE.search(cover_text)
    if gazette_cover:
        number = gazette_cover.group(1).strip().title()
        when = spoken_date(_ordinal_free(gazette_cover.group(2)))
        issue = {**issue, "number": number, **({"date": when} if when else {})}
        said("publication.number", number, cover_page, gazette_cover.group(0))
        if when:
            said("publication.date", when, cover_page, gazette_cover.group(0))
    translated = COVER_TRANSLATED.search(cover_text)
    legal: dict = {}
    # A text whose steps are events (a regulation made, an instruction signed) commences as an event.
    commenced = "event:commenced.date" if by_events else "step:commenced.date"
    if issue.get("date"):
        published = "event:published.date" if by_events else "step:published.date"
        values[published] = issue["date"]
        values["publication.date"] = issue["date"]
        if "publication.date" not in evidence:
            said("publication.date", issue["date"], None, f"the issue: {issue.get('name') or ''}".strip())
        said(
            published,
            issue["date"],
            evidence["publication.date"]["page"],
            evidence["publication.date"]["evidence"],
        )
        in_force = stated(worded, IN_FORCE_ON_PUBLICATION)
        if in_force:
            legal[commenced] = {"value": issue["date"], **in_force}
            if values["identity.family"] in ("legislation", "constitution"):
                legal["profile.commencement"] = {"value": "on_publication", **in_force}
    # A text that comes into force on the day it is signed commences on the date above its signatures.
    signed_on = next(
        (
            values[key]
            for key in ("step:signed.date", "step:promulgated.date", "event:signed.date", "event:made.date")
            if key in values
        ),
        None,
    )
    on_signature = stated(worded, IN_FORCE_ON_SIGNATURE) if signed_on else None
    if on_signature and commenced not in legal:
        legal[commenced] = {"value": signed_on, **on_signature}
    values["publication.kind"] = "gazette"
    values["publication.name"] = GAZETTE
    edition = (
        str(issue["number"])
        if str(issue.get("number") or "").lower().startswith("special")
        else "Special"
        if issue.get("special")
        else str(issue.get("number") or "")
    )
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
    stating = stated(paragraphs, ADOPTED_IN) if authority else None
    if stating:
        legal["expressions"] = {
            "authority": {
                row["language"]: {
                    "value": "authoritative" if row["language"] == authority else "official_translation",
                    **stating,
                }
                for row in values["expressions"]
            }
        }
    elif translated:
        # "Translated into Kinyarwanda from French and English": those are the texts adopted.
        originals = {m.lower()[:2] for m in translated.groups() if m}
        originals = {"fr" if o == "fr" else "en" for o in originals}
        legal["expressions"] = {
            "authority": {
                row["language"]: {
                    "value": "authoritative" if row["language"] in originals else "official_translation",
                    "page": cover_page,
                    "evidence": translated.group(0),
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
        said("text.summary.text", purpose["text"][:300], purpose.get("page"), purpose["text"])
    for key in ("publication.name", "publication.kind", "publication.publisher", "text.form", "expressions"):
        if key in values and key not in evidence:
            said(key, values[key], None, f"the Official Gazette issue: {issue.get('name') or ''}".strip())
    return values, legal, inferred, evidence


def versions_nodes(lines: list[dict], language: str = "en") -> list[dict]:
    """A version's paragraphs (the English one's, unless named), each with the article it follows."""
    nodes = structure(lines, language) if lines else []
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
