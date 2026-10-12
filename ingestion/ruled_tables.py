"""Tables a PDF draws with ruling lines, rebuilt from those lines and the page's own words.

A gazette table is drawn as thin filled rectangles (or stroked segments) for its cell borders; the
words in it are in the text layer. The rules give the grid, a border missing between two grid
cells merges them (a heading over two columns, a label over two rows), and each word goes to the
cell its centre lies in. The text is exactly as printed, typing slips included; nothing is read
again by a model.

Rules are read from the page's content stream with pypdf: the operators that build a path ("re",
"m", "l") and paint it ("f", "S", …), each point placed by the current transformation matrix,
clipping paths ("W n") ignored. Rules drawn inside form XObjects are not yet followed.
"""

from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ContentStream

PAINT = {b"f", b"f*", b"F", b"B", b"B*", b"b", b"b*", b"S", b"s"}
THIN = 2.5  # a rule is at most this thick, in points


def _times(m: list[float], n: list[float]) -> list[float]:
    """The matrix m then n, as PDF concatenates them ("cm" puts m before the current matrix)."""
    return [
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    ]


def rules(page) -> list[tuple[float, float, float, float]]:
    """Every ruling line drawn on a pypdf page, as (x0, y0, x1, y1) with the origin at top left.
    The content stream is read directly (pypdf's text extraction skips pages without fonts)."""
    height = float(page.mediabox.height)
    contents = page.get_contents()
    if contents is None:
        return []
    matrix, saved = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], []
    pending: list[tuple] = []
    found: list[tuple[float, float, float, float]] = []

    def place(x, y):
        a, b, c, d, e, f = matrix
        return a * x + c * y + e, b * x + d * y + f

    for operands, operator in ContentStream(contents, page.pdf).operations:
        if operator == b"q":
            saved.append(matrix)
        elif operator == b"Q":
            matrix = saved.pop() if saved else matrix
        elif operator == b"cm":
            matrix = _times([float(v) for v in operands], matrix)
        elif operator == b"re":
            x, y, w, h = (float(v) for v in operands)
            pending.append(("rect", place(x, y), place(x + w, y + h)))
        elif operator == b"m":
            pending.append(("move", place(float(operands[0]), float(operands[1]))))
        elif operator == b"l":
            pending.append(("line", place(float(operands[0]), float(operands[1]))))
        elif operator in PAINT:
            last = None
            for item in pending:
                if item[0] == "rect":
                    (x0, y0), (x1, y1) = item[1], item[2]
                    x0, x1 = sorted((x0, x1))
                    y0, y1 = sorted((y0, y1))
                    if min(x1 - x0, y1 - y0) <= THIN and max(x1 - x0, y1 - y0) >= 4:
                        found.append((x0, height - y1, x1, height - y0))
                elif item[0] == "move":
                    last = item[1]
                elif item[0] == "line" and last is not None:
                    (x0, y0), (x1, y1) = last, item[1]
                    if abs(x0 - x1) < 1 or abs(y0 - y1) < 1:
                        found.append((min(x0, x1), height - max(y0, y1), max(x0, x1), height - min(y0, y1)))
                    last = item[1]
            pending.clear()
        elif operator == b"n":  # a clipping path, or one never painted
            pending.clear()
    return found


def _horizontal(rule) -> bool:
    return rule[3] - rule[1] < 3


def frames(found: list[tuple], tolerance: float = 2.0) -> list[list[tuple]]:
    """Rules that touch, grouped: each group frames one table."""
    groups: list[list[tuple]] = []
    for rule in found:
        touching = [
            group
            for group in groups
            if any(
                rule[0] - tolerance <= other[2]
                and other[0] - tolerance <= rule[2]
                and rule[1] - tolerance <= other[3]
                and other[1] - tolerance <= rule[3]
                for other in group
            )
        ]
        merged = [rule]
        for group in touching:
            merged += group
            groups.remove(group)
        groups.append(merged)
    return groups


def _positions(values: list[float], tolerance: float = 1.5) -> list[float]:
    runs: list[list[float]] = []
    for value in sorted(values):
        if runs and value - runs[-1][-1] <= tolerance:
            runs[-1].append(value)
        else:
            runs.append([value])
    return [sum(run) / len(run) for run in runs]


def table(frame: list[tuple], words: list[dict]) -> dict | None:
    """The table one frame draws: its cells with their spans and words, rows in order."""
    across = [r for r in frame if _horizontal(r)]
    down = [r for r in frame if r[2] - r[0] < 3]
    ys = _positions([(r[1] + r[3]) / 2 for r in across])
    xs = _positions([(r[0] + r[2]) / 2 for r in down])
    if len(ys) < 2 or len(xs) < 2:
        return None

    def border_down(x, top, bottom):
        middle = (top + bottom) / 2
        return any(abs((r[0] + r[2]) / 2 - x) < 1.5 and r[1] - 1 <= middle <= r[3] + 1 for r in down)

    def border_across(y, left, right):
        middle = (left + right) / 2
        return any(abs((r[1] + r[3]) / 2 - y) < 1.5 and r[0] - 1 <= middle <= r[2] + 1 for r in across)

    rows, columns = len(ys) - 1, len(xs) - 1
    parent = {(i, j): (i, j) for i in range(rows) for j in range(columns)}

    def root(unit):
        while parent[unit] != unit:
            parent[unit] = parent[parent[unit]]
            unit = parent[unit]
        return unit

    for i in range(rows):
        for j in range(columns):
            if j + 1 < columns and not border_down(xs[j + 1], ys[i], ys[i + 1]):
                parent[root((i, j + 1))] = root((i, j))
            if i + 1 < rows and not border_across(ys[i + 1], xs[j], xs[j + 1]):
                parent[root((i + 1, j))] = root((i, j))
    cells: dict[tuple, list[tuple]] = {}
    for unit in parent:
        cells.setdefault(root(unit), []).append(unit)
    placed = []
    for units in cells.values():
        top, bottom = min(u[0] for u in units), max(u[0] for u in units)
        left, right = min(u[1] for u in units), max(u[1] for u in units)
        box = (xs[left], ys[top], xs[right + 1], ys[bottom + 1])
        inside = [
            w
            for w in words
            if box[0] <= (w["xmin"] + w["xmax"]) / 2 < box[2]
            and box[1] <= (w["ymin"] + w["ymax"]) / 2 < box[3]
        ]
        inside.sort(key=lambda w: (round((w["ymin"] + w["ymax"]) / 8), w["xmin"]))
        placed.append(
            {
                "row": top,
                "column": left,
                "row_span": bottom - top + 1,
                "col_span": right - left + 1,
                "text": " ".join(w["text"] for w in inside),
                "words": inside,
            }
        )
    by_row = [sorted((c for c in placed if c["row"] == i), key=lambda c: c["column"]) for i in range(rows)]
    by_row = [row for row in by_row if row]
    # The header: the first row and the rows its tallest cell reaches down over.
    header = max((c["row_span"] for c in by_row[0]), default=1) if by_row else 0
    strip = [[{k: c[k] for k in ("text", "row_span", "col_span")} for c in row] for row in by_row]
    return {
        "box": (xs[0], ys[0], xs[-1], ys[-1]),
        "header_rows": strip[:header] if len(strip) > header else [],
        "body_rows": strip[header:] if len(strip) > header else strip,
        "words": [w for c in placed for w in c["words"]],
        "columns": columns,
    }


def page_tables(page, words: list[dict], width: float, language_columns: int = 1) -> list[dict]:
    """The content tables on one page. On a page printed in language columns, a frame as wide as
    the page is the frame of the columns themselves, not a table in the law."""
    out = []
    for frame in frames(rules(page)):
        found = table(frame, words)
        if not found:
            continue
        x0, y0, x1, y1 = found["box"]
        if x1 - x0 < 30 or y1 - y0 < 12:
            continue
        if language_columns > 1 and x1 - x0 > 0.45 * width:
            continue
        if found["columns"] < 2 and len(found["body_rows"]) + len(found["header_rows"]) < 2:
            continue  # a box around a paragraph, not a table
        out.append(found)
    return out


def document_tables(pdf: Path, pages: list[dict], layouts: list[int]) -> dict[int, list[dict]]:
    """Content tables of every page of a PDF, keyed by page number; `pages` are its text-layer words
    per page and `layouts` the number of language columns measured on each."""
    reader = PdfReader(str(pdf))
    found = {}
    for number, (page, words) in enumerate(zip(reader.pages, pages, strict=False), 1):
        tables = page_tables(page, words["words"], words["width"], layouts[number - 1])
        if tables:
            found[number] = tables
    return found


def image_coverage(page) -> float:
    """How much of a pypdf page its images cover (0 to 1): a page that is one picture is a scan,
    whatever text layer someone has laid over it."""
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    resources = page.get("/Resources") or {}
    images = resources.get("/XObject") or {}
    contents = page.get_contents()
    if contents is None or not width or not height:
        return 0.0
    matrix, saved, area = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], [], 0.0
    for operands, operator in ContentStream(contents, page.pdf).operations:
        if operator == b"q":
            saved.append(matrix)
        elif operator == b"Q":
            matrix = saved.pop() if saved else matrix
        elif operator == b"cm":
            matrix = _times([float(v) for v in operands], matrix)
        elif operator == b"Do":
            found = images.get(operands[0])
            found = found.get_object() if found is not None else None
            if found is not None and found.get("/Subtype") == "/Image":
                area += abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])
    return min(1.0, area / (width * height))


def image_pages(pdf: Path, threshold: float = 0.6) -> set[int]:
    """Pages of a PDF that are pictures of a page (scans), numbered from 1."""
    return {n for n, page in enumerate(PdfReader(str(pdf)).pages, 1) if image_coverage(page) > threshold}
