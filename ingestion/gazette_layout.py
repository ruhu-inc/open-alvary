"""How a gazette page is laid out, before it is read: text layer or scan, columns, languages.

Rwanda's Official Gazette prints Kinyarwanda, English and French; whether a page carries them side by
side in columns or one after another, and whether its text layer can be trusted, decides how it is
read. This probe takes every word the PDF's own text layer holds, with its position (poppler's
`pdftotext -bbox-layout`, run in the sealed container used for OCR: no network, read-only input),
finds the columns from the gaps no line crosses, and guesses each column's language from its words.
It reads nothing aloud to any service; it only measures.
"""

import re
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

SANDBOX_IMAGE = "open-alvary-ocr"

# Words that mark a language in legislative text (lower-case, whole words).
MARKERS = {
    "rw": {
        "ya",
        "na",
        "mu",
        "ku",
        "cyangwa",
        "ingingo",
        "itegeko",
        "iteka",
        "kandi",
        "ry",
        "rw",
        "by",
        "bya",
        "rya",
        "ni",
        "iyo",
        "ibi",
        "uko",
        "rwanda",
        "repubulika",
        "y",
        "w",
        "z",
        "ishyirwaho",
        "minisitiri",
    },
    "en": {
        "the",
        "of",
        "and",
        "article",
        "shall",
        "law",
        "this",
        "is",
        "in",
        "to",
        "by",
        "order",
        "be",
        "which",
        "or",
        "on",
        "chapter",
        "section",
        "minister",
        "republic",
    },
    "fr": {
        "de",
        "la",
        "le",
        "et",
        "des",
        "les",
        "du",
        "loi",
        "article",
        "est",
        "en",
        "par",
        "au",
        "aux",
        "une",
        "un",
        "sur",
        "chapitre",
        "arrêté",
        "ministre",
        "république",
    },
}


class _Words(HTMLParser):
    """Pages and their words from pdftotext's -bbox-layout output."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pages: list[dict] = []
        self._word: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "page":
            self.pages.append({"width": float(attrs["width"]), "height": float(attrs["height"]), "words": []})
        elif tag == "word" and self.pages:
            self._word = {k: float(attrs[k]) for k in ("xmin", "ymin", "xmax", "ymax")} | {"text": ""}

    def handle_endtag(self, tag):
        if tag == "word" and self._word is not None:
            self.pages[-1]["words"].append(self._word)
            self._word = None

    def handle_data(self, data):
        if self._word is not None:
            self._word["text"] += data


def words(bbox_html: str) -> list[dict]:
    parser = _Words()
    parser.feed(bbox_html)
    pages = parser.pages
    # A page with no size is a reference to a page missing from a damaged file (RLRC's 2003
    # Constitution lists 60 pages and holds 49); poppler repeats another page's words on it. It
    # keeps its place, so the pages after it keep their numbers, but nothing is read from it.
    sized = [p for p in pages if p["width"] and p["height"]]
    for page in pages:
        if not page["width"] or not page["height"]:
            like = sized[-1] if sized else {"width": 595.0, "height": 842.0}  # A4 if none is sized
            page.update(width=like["width"], height=like["height"], words=[], missing=True)
    return pages


def from_ocr(reading: dict, page: dict) -> dict:
    """A scanned page with the words Google's OCR read on it, placed as a text layer places them
    (points from the top left of the page), in place of whatever text layer the scan carries."""
    width, height = page["width"], page["height"]
    words = [
        {
            "xmin": w["x0"] * width,
            "ymin": w["y0"] * height,
            "xmax": w["x1"] * width,
            "ymax": w["y1"] * height,
            "text": w["text"],
        }
        for w in reading["words"]
    ]
    return {**page, "words": words, "read_by": "ocr"}


def language(texts: list[str]) -> tuple[str | None, dict]:
    """The language whose marker words are most frequent, with the counts behind the guess."""
    tokens = [t for text in texts for t in re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", text.lower())]
    counts = Counter()
    for token in tokens:
        for code, markers in MARKERS.items():
            if token.strip("'") in markers:
                counts[code] += 1
    if not counts:
        return None, {}
    best, score = counts.most_common(1)[0]
    total = sum(counts.values())
    return (best if score >= 3 and score / total >= 0.5 else None), dict(counts)


def columns(page: dict, min_gap: float = 0.004, bins: int = 400) -> list[tuple[float, float]]:
    """Column spans from the vertical gaps that (almost) no line crosses; the Gazette's gutters
    between its language columns are about 6 pt."""
    width = page["width"]
    if not page["words"]:
        return []
    cover = [0] * bins
    for word in page["words"]:
        first = max(0, int(word["xmin"] / width * bins))
        last = min(bins - 1, int(word["xmax"] / width * bins))
        for b in range(first, last + 1):
            cover[b] += 1
    occupied = [b for b, n in enumerate(cover) if n > 0]
    if not occupied:
        return []
    left, right = occupied[0], occupied[-1]
    threshold = max(1, len(page["words"]) * 0.004)  # a stray word or two may cross a gutter
    spans, start = [], left
    b = left
    while b <= right:
        if cover[b] < threshold:
            gap_start = b
            while b <= right and cover[b] < threshold:
                b += 1
            if (b - gap_start) / bins >= min_gap and gap_start > start:
                spans.append((start, gap_start))
                start = b
        b += 1
    spans.append((start, right + 1))
    spans = [(s / bins * width, e / bins * width) for s, e in spans if e > s]
    # Discard slivers (page numbers, marginal marks) narrower than a tenth of the text width.
    text_width = (right - left) / bins * width
    return [(s, e) for s, e in spans if (e - s) >= 0.1 * text_width]


def analyse(pages: list[dict]) -> list[dict]:
    out = []
    for number, page in enumerate(pages, 1):
        spans = columns(page)
        cols = []
        for x0, x1 in spans:
            inside = [w["text"] for w in page["words"] if x0 <= (w["xmin"] + w["xmax"]) / 2 < x1]
            lang, counts = language(inside)
            cols.append(
                {
                    "x0": round(x0, 1),
                    "x1": round(x1, 1),
                    "words": len(inside),
                    "language": lang,
                    "markers": counts,
                }
            )
        n = len(page["words"])
        out.append(
            {
                "page": number,
                "words": n,
                "text_layer": n >= 20,
                "columns": cols,
                "layout": "no_text"
                if n < 20
                else {1: "single", 2: "two_columns", 3: "three_columns"}.get(
                    len(cols), f"{len(cols)}_columns"
                ),
                "languages": [c["language"] for c in cols],
            }
        )
    return out


def text_layer(pdf: Path) -> list[dict]:
    """Every page's words with their positions, from the PDF's text layer, read inside the sealed
    container (no network, read-only input)."""
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "-v",
            f"{Path(pdf).resolve()}:/in/doc.pdf:ro",
            SANDBOX_IMAGE,
            "pdftotext -bbox-layout -enc UTF-8 /in/doc.pdf -",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    return words(completed.stdout)


def probe(pdf: Path) -> list[dict]:
    """Per-page layout of a PDF, measured from its text layer inside the sealed container."""
    return analyse(text_layer(pdf))


def summary(pages: list[dict]) -> dict:
    layouts = Counter(p["layout"] for p in pages)
    orders = Counter(tuple(p["languages"]) for p in pages if p["layout"] == "three_columns")
    return {
        "pages": len(pages),
        "with_text_layer": sum(p["text_layer"] for p in pages),
        "layouts": dict(layouts),
        "three_column_language_orders": {" | ".join(map(str, k)): v for k, v in orders.most_common()},
    }
