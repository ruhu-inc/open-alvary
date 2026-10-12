"""Tables drawn without lines (a budget's annexes), rebuilt from where their words stand.

The rows of such a table end in amounts set flush right in one or more columns, and many begin with
a code whose indent gives its level (a chapter "21", its item "211", the sub-item "2113"). The amount
columns are found from the right edges of the amounts, the code columns from where the codes begin,
the description is what lies between, and the heading is read from the lines just above the first
row, each word into the column it stands over. The words are the page's own, as printed.
"""

import re

AMOUNT = re.compile(r"^\(?-?\d{1,3}(?:[,.  ]\d{3})*(?:[.,]\d+)?\)?%?$|^-$")
CODE = re.compile(r"^[A-Z]{0,2}\d{1,6}[A-Z]?$")
MIN_ROWS = 4


def _runs(values: list[float], tolerance: float) -> list[list[float]]:
    runs: list[list[float]] = []
    for value in sorted(values):
        if runs and value - runs[-1][-1] <= tolerance:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def _row_words(words: list[dict]) -> list[list[dict]]:
    """Words in printed lines, each line left to right."""
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["ymin"] + w["ymax"]) / 2):
        centre = (word["ymin"] + word["ymax"]) / 2
        if rows and abs(centre - sum((w["ymin"] + w["ymax"]) / 2 for w in rows[-1]) / len(rows[-1])) <= 3:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: w["xmin"]) for row in rows]


def _body(rows: list[list[dict]], width: float) -> list[int]:
    return [
        i
        for i, row in enumerate(rows)
        if AMOUNT.match(row[-1]["text"]) and row[-1]["xmax"] > 0.55 * width and len(row) >= 2
    ]


def signature(words: list[dict], width: float) -> tuple[str, list[float]] | None:
    """What a page's table shares with the other pages of the same table: its heading words, and
    where its codes begin."""
    rows = _row_words(words)
    body = _body(rows, width)
    if len(body) < MIN_ROWS:
        return None
    heading = " ".join(w["text"] for i in range(max(0, body[0] - 3), body[0]) for w in rows[i])
    starts = [rows[i][0]["xmin"] for i in body if CODE.match(rows[i][0]["text"])]
    return heading, starts


def document_code_columns(pages: list[tuple[list[dict], float]]) -> dict[str, list[float]]:
    """Code columns per table heading, from every page that carries that heading: a page on which
    only some levels occur still places its codes in the right columns."""
    starts: dict[str, list[float]] = {}
    for words, width in pages:
        found = signature(words, width)
        if found:
            starts.setdefault(found[0], []).extend(found[1])
    return {heading: [min(run) for run in _runs(xs, 4.0) if len(run) >= 2] for heading, xs in starts.items()}


def page_table(words: list[dict], width: float, code_columns: list[float] | None = None) -> dict | None:
    """The one table of lined-up amounts on a page, or None if there is none; `code_columns`, when
    given, are where the codes of this table begin across all its pages."""
    rows = _row_words(words)
    body = _body(rows, width)
    if len(body) < MIN_ROWS:
        return None
    first, last = body[0], body[-1]
    amounts = [w for i in body for w in rows[i] if AMOUNT.match(w["text"]) and w["xmin"] > 0.45 * width]
    columns = [run for run in _runs([w["xmax"] for w in amounts], 6.0) if len(run) >= max(2, len(body) // 5)]
    rights = [max(run) for run in columns]
    widest = max((w["xmax"] - w["xmin"] for w in amounts), default=40)
    codes = [rows[i][0] for i in range(first, last + 1) if CODE.match(rows[i][0]["text"])]
    code_columns = code_columns or [min(run) for run in _runs([w["xmin"] for w in codes], 4.0)]
    amount_left = min(r - widest - 4 for r in rights) if rights else width

    def place(word) -> tuple[str, int]:
        centre = (word["xmin"] + word["xmax"]) / 2
        if word["xmin"] >= amount_left and AMOUNT.match(word["text"]) or centre >= amount_left:
            return "amount", min(range(len(rights)), key=lambda k: abs(rights[k] - word["xmax"]))
        return "text", 0

    table_rows = []
    for i in range(first, last + 1):
        row = rows[i]
        cells = {"codes": [""] * len(code_columns), "description": [], "amounts": [""] * len(rights)}
        rest = row
        if CODE.match(row[0]["text"]) and code_columns:
            k = min(range(len(code_columns)), key=lambda k: abs(code_columns[k] - row[0]["xmin"]))
            cells["codes"][k] = row[0]["text"]
            rest = row[1:]
        for word in rest:
            kind, k = place(word)
            if kind == "amount" and rights:
                cells["amounts"][k] = (cells["amounts"][k] + " " + word["text"]).strip()
            else:
                cells["description"].append(word["text"])
        if not any(cells["codes"]) and not any(cells["amounts"]) and table_rows:
            table_rows[-1][len(code_columns)] += " " + " ".join(cells["description"])  # a wrapped description
            continue
        table_rows.append([*cells["codes"], " ".join(cells["description"]), *cells["amounts"]])

    # The heading: the lines just above the first row, each word under its column.
    header_lines = []
    for i in range(first - 1, -1, -1):
        above = rows[i]
        gap = rows[i + 1][0]["ymin"] - max(w["ymax"] for w in above)
        if gap > 25 or len(header_lines) >= 4 or re.match(r"^(ANNEX|UMUGEREKA)", above[0]["text"]):
            break
        header_lines.insert(0, above)
    header = [""] * (len(code_columns) + 1 + len(rights))
    bounds = [*code_columns, (code_columns[-1] + 30) if code_columns else 0]
    for line in header_lines:
        for word in line:
            centre = (word["xmin"] + word["xmax"]) / 2
            if rights and centre >= amount_left - 10:
                k = (
                    len(code_columns)
                    + 1
                    + min(range(len(rights)), key=lambda k: abs(rights[k] - word["xmax"]))
                )
            elif code_columns and centre < bounds[-1]:
                k = min(range(len(code_columns)), key=lambda k: abs(code_columns[k] - word["xmin"]))
            else:
                k = len(code_columns)
            header[k] = (header[k] + " " + word["text"]).strip()
    used = {id(w) for i in range(first, last + 1) for w in rows[i]} | {
        id(w) for line in header_lines for w in line
    }
    top = min(w["ymin"] for line in (header_lines or [rows[first]]) for w in line)
    cell = lambda text: {"text": text, "row_span": 1, "col_span": 1}  # noqa: E731
    return {
        "box": (
            min(w["xmin"] for i in range(first, last + 1) for w in rows[i]),
            top,
            max(w["xmax"] for i in range(first, last + 1) for w in rows[i]),
            max(w["ymax"] for w in rows[last]),
        ),
        "header_rows": [[cell(t) for t in header]] if any(header) else [],
        "body_rows": [[cell(t) for t in row] for row in table_rows],
        "words": [w for line in rows for w in line if id(w) in used],
        "columns": len(header),
    }
