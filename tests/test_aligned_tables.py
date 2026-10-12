"""Tables drawn without lines (a budget's annexes) rebuilt from where their words stand. The figures
and items are invented, set out as the Gazette sets them."""

from ingestion import aligned_tables


def line(y, *placed):
    """Words on one printed line: (x, text) pairs; amounts end at their x (set flush right)."""
    out = []
    for x, text in placed:
        width = 5.0 * len(text)
        right = aligned_tables.AMOUNT.match(text) and x > 300
        x0 = x - width if right else x
        out.append({"xmin": x0, "xmax": x0 + width, "ymin": y, "ymax": y + 9, "text": text})
    return out


def budget_page(rows):
    words = line(100, (200, "ANNEX"), (240, "II-1:"), (270, "DETAILED"), (320, "BUDGET"))
    words += line(140, (24, "BA"), (40, "Prog"), (90, "Chap"), (120, "Sub"), (150, "Eco"), (520, "Total"))
    words += line(150, (120, "Chap"), (150, "Item"), (520, "Budget"))
    for k, row in enumerate(rows):
        words += line(165 + 12 * k, *row)
    return words


ROWS = [
    [(45, "05"), (62, "Operations"), (110, "And"), (130, "Services"), (586, "43,344,139,866")],
    [(107, "21"), (116, "Compensation"), (180, "Of"), (196, "Employees"), (586, "19,494,852,873")],
    [(122, "211"), (139, "Wages"), (170, "and"), (190, "Salaries"), (586, "19,494,852,873")],
    [(157, "2113"), (174, "Wages"), (205, "for"), (225, "Other"), (586, "19,494,852,873")],
    [(107, "22"), (116, "Use"), (140, "Of"), (155, "Goods"), (586, "2,571,207,005")],
]


def test_codes_go_to_their_level_descriptions_between_and_amounts_to_their_column():
    table = aligned_tables.page_table(budget_page(ROWS), 612)
    assert [[c["text"] for c in row] for row in table["body_rows"]] == [
        ["05", "", "", "", "Operations And Services", "43,344,139,866"],
        ["", "21", "", "", "Compensation Of Employees", "19,494,852,873"],
        ["", "", "211", "", "Wages and Salaries", "19,494,852,873"],
        ["", "", "", "2113", "Wages for Other", "19,494,852,873"],
        ["", "22", "", "", "Use Of Goods", "2,571,207,005"],
    ]
    header = [c["text"] for c in table["header_rows"][0]]
    assert header[-1] == "Total Budget" and "Sub Chap" in header


def test_a_page_where_only_some_levels_occur_takes_its_columns_from_the_other_pages():
    full = budget_page(ROWS + ROWS)  # every level met more than once, as across a budget's pages
    partial = budget_page([ROWS[2], ROWS[3], ROWS[2], ROWS[3]])
    columns = aligned_tables.document_code_columns([(full, 612), (partial, 612)])
    heading = aligned_tables.signature(partial, 612)[0]
    table = aligned_tables.page_table(partial, 612, columns[heading])
    assert [c["text"] for c in table["body_rows"][0]][:4] == ["", "", "211", ""]


def test_ordinary_text_with_a_number_at_a_line_end_is_not_a_table():
    words = []
    for k in range(6):
        words += line(
            100 + 12 * k, (60, "The"), (90, "fine"), (120, "is"), (140, "not"), (170, "less"), (200, "than")
        )
    assert aligned_tables.page_table(words, 612) is None
