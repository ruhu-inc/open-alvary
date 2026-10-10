"""Word-level comparison shared by the reading pipeline and the reader benchmark.

Tokens are split on whitespace exactly as the review page renders them, so a flagged token index
always points at the word a reviewer sees. Each token is compared through a key that ignores
typography a reader cannot be expected to reproduce (curly quotes, dash variants, dot leaders)
but keeps every letter, digit and bracket.
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

TOKEN = re.compile(r"\S+")
QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "—", "―": "—"})
LEADERS = re.compile(r"\.{2,}|…+")
NUMBERISH = re.compile(r"\d|^\(?[a-z]{1,2}\)$|^\(?[ivxl]{1,6}\)$")
LOOSE = re.compile(r"[^0-9a-z]+")


DASH = re.compile(r"^-{1,4}$")


FILLER = re.compile(r"^[.…_·•]+$")  # dot leaders and blank fill-in lines: printed, never words


def key(token: str) -> str:
    """Comparison key for one rendered token: typography folded, case kept; "" means ignore it."""
    token = unicodedata.normalize("NFKC", token).translate(QUOTES).replace("₦", "N")  # Naira sign read as N
    if DASH.match(token):
        return "—"  # readers write a printed dash as -, -- or ---
    if FILLER.match(token):
        return ""
    return LEADERS.sub("", token)


def loose(token: str) -> str:
    """Letters and digits only, lower case: equal loose keys differ only in typography or case."""
    return LOOSE.sub("", key(token).lower())


@dataclass
class Flag:
    severity: str  # high | medium | low
    kind: str  # differs | only_in_primary | missing_from_primary | moved | marker_conflict
    primary: str
    checks: dict[str, str]
    tokens: tuple[int, int]  # primary token range [start, end); empty for missing words
    readers_disagreeing: list[str] = field(default_factory=list)


MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}


def _severity(primary: list[str], others: list[list[str]], kind: str) -> str:
    words = primary + [w for other in others for w in other]
    if any(NUMBERISH.search(key(w).lower()) or loose(w) in MONTHS for w in words):
        return "high"  # numbers, dates (months included), section and paragraph references
    if kind == "only_in_primary" and len(primary) >= 2:
        return "high"  # several words no independent reading saw: possible reconstruction
    if others and all(
        "".join(loose(w) for w in primary) == "".join(loose(w) for w in other) for other in others
    ):
        return "low"  # typography or case only
    return "medium"


def _contains(haystack: list[str], needle: list[str]) -> bool:
    size = len(needle)
    return size > 0 and any(haystack[i : i + size] == needle for i in range(len(haystack) - size + 1))


def compare(primary: str, checks: dict[str, str]) -> list[Flag]:
    """Flag primary tokens that the independent check readings do not support.

    A span is flagged when every check reading disagrees with it; when the checks split, the
    span is flagged one level lower and names the readers that disagree.
    """
    tokens = TOKEN.findall(primary)
    keys = [key(t).lower() for t in tokens]
    # Fillers (dot leaders, blank lines) take part in nothing: compare only real words.
    real = [i for i, k in enumerate(keys) if k]
    disagree: list[set[str]] = [set() for _ in tokens]
    other_keys = {
        name: [k for k in (key(t).lower() for t in TOKEN.findall(text)) if k] for name, text in checks.items()
    }
    replaced: dict[str, dict[int, str]] = {}
    missing: dict[str, dict[int, str]] = {}
    for name, text in checks.items():
        other = [t for t in TOKEN.findall(text) if key(t)]
        matcher = difflib.SequenceMatcher(a=[keys[i] for i in real], b=other_keys[name], autojunk=False)
        replaced[name], missing[name] = {}, {}
        at = lambda position: real[position] if position < len(real) else len(tokens)  # noqa: E731
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            for i in range(i1, i2):
                disagree[real[i]].add(name)
            if tag == "replace":
                replaced[name][at(i1)] = " ".join(other[j1:j2])
            elif tag == "insert":
                missing[name][at(i1)] = " ".join(other[j1:j2])
    flags, i, names = [], 0, list(checks)
    while i < len(tokens):
        if not disagree[i]:
            i += 1
            continue
        start, who = i, disagree[i]
        while i < len(tokens) and disagree[i] == who:
            i += 1
        span = tokens[start:i]
        seen = {n: replaced[n].get(start, "") for n in names if n in who}
        kind = "differs" if any(seen.values()) else "only_in_primary"
        if (
            kind == "only_in_primary"
            and len(span) >= 2
            and all(_contains(other_keys[n], keys[start:i]) for n in who)
        ):
            # Every disagreeing reading has these words elsewhere on the page (a margin note or
            # heading placed differently): the words are confirmed, only their position differs.
            flags.append(Flag("low", "moved", " ".join(span), seen, (start, i), sorted(who)))
            continue
        severity = _severity(span, [v.split() for v in seen.values() if v], kind)
        if len(who) < len(names):
            severity = {"high": "medium", "medium": "low", "low": "low"}[severity]
        flags.append(Flag(severity, kind, " ".join(span), seen, (start, i), sorted(who)))
    positions = sorted({p for n in names for p in missing[n]})
    for position in positions:
        seen = {n: missing[n][position] for n in names if position in missing[n]}
        if len(seen) < len(names):
            continue  # only one check reading has extra words here: too weak to flag
        if all(
            len(v.split()) >= 2 and _contains(keys, [key(t).lower() for t in v.split()])
            for v in seen.values()
        ):
            continue  # the primary has these words elsewhere on the page: moved, already counted
        severity = _severity([], [v.split() for v in seen.values()], "missing_from_primary")
        flags.append(Flag(severity, "missing_from_primary", "", seen, (position, position), sorted(seen)))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(flags, key=lambda f: (order[f.severity], f.tokens))


class HtmlTables(HTMLParser):
    """HTML tables as cell grids; spanned columns keep empty placeholders."""

    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            self.tables.append([])
        elif tag == "tr" and self.depth:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
            span = int(dict(attrs).get("colspan") or 1)
            self.row.extend([None] * (span - 1))  # placeholders so spanned columns still count
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.depth:
            self.tables[-1].append(["" if c is None else c for c in self.row])
            self.row = None
        elif tag == "table":
            self.depth = max(self.depth - 1, 0)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
