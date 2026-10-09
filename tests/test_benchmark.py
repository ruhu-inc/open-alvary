from ingestion import benchmark as b


def test_normalisation_ignores_typography_but_not_words():
    assert b.words("“the Board”) ; (f ) 18TH.... Day of FEBRUARY…, 202..6") == [
        '"the',
        'Board");',
        "(f)",
        "18TH",
        "Day",
        "of",
        "FEBRUARY,",
        "2026",
    ]
    assert b.words("Act—(1)") == b.words("Act – (1)") == ["Act", "—", "(1)"]


def test_html_tables_stay_in_reading_order_with_spans():
    markup = "# SCHEDULE\n<table><tr><th colspan=2>A</th><th>B</th></tr><tr><td>x</td><td>y</td><td>z</td></tr></table>\nAfter"
    reading = b.from_markup(markup)
    assert reading["tables"] == [[["", "A", "B"], ["x", "y", "z"]]]
    assert reading["text"].split() == ["SCHEDULE", "A", "B", "x", "y", "z", "After"]


def test_pipe_tables_are_parsed_and_rules_dropped():
    reading = b.from_markup("Intro\n| a | b |\n|---|---|\n| 1 | 2 |\n**Done**")
    assert reading["tables"] == [[["a", "b"], ["1", "2"]]]
    assert reading["text"].split() == ["Intro", "a", "b", "1", "2", "Done"]


def test_scores_count_missing_words_and_recasing():
    gold = {"text": "SCHEDULE TO THE BILL 08 Day of June", "tables": []}
    result = b.score(gold, {"text": "Schedule to the Bill 8th Day of June", "tables": []})
    assert result["recased"] == 4
    assert round(result["recall"], 3) == round(7 / 8, 3)
    assert result["word_error_rate"] == 1 / 8


def test_table_score_requires_matching_columns():
    gold = [["SHORT TITLE", "DATE"], ["Electricity Bill, 2023", "10th May, 2023"]]
    good = b.table_score(gold, [[["SHORT TITLE", "DATE"], ["Electricity Bill, 2023", "10th May, 2023"]]])
    assert good == {"found": True, "columns": 2, "cells": 1.0}
    flattened = b.table_score(gold, [[["SHORT TITLE DATE Electricity Bill, 2023 10th May, 2023"]]])
    assert flattened["cells"] == 0.0
