"""Exact metadata normalization, and the catalogue's word search (G11: never part of a word, never a
scan for a substring).

normalize_citation compares whole values (a citation, an alias, a title) exactly. A catalogue search
finds a record when every word of the query is a word of its title, citation or summary, in any order.
Words are runs of letters and digits in NFKC and lower case; punctuation separates them. On PostgreSQL
they are the 'simple' text search analysis (no stemming, no common words left out) of exactly the
expression migration 0038 indexes (VECTOR), so the match is read from a GIN index. SQLite, the
metadata-only pilot and the fast tests, compares the same words in Python (has_every_word).
"""

import re
import sqlite3
import unicodedata

from sqlalchemy import event
from sqlalchemy.engine import Engine

# PostgreSQL's words of a text expression, as migration 0038 indexes them for a source's title,
# citation and summary together.
VECTOR = "to_tsvector('simple', regexp_replace(normalize({}, NFKC), '[[:punct:]]+', ' ', 'g'))"
FIELDS = ("title", "citation", "summary")


def normalize_citation(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def words(value: str | None) -> list[str]:
    """A value's words as a catalogue search reads them."""
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", value or "").lower()).split()


def has_every_word(value: str | None, query: str | None) -> bool:
    wanted = words(query)
    return bool(wanted) and set(wanted) <= set(words(value))


def fields_vector(table: str, fields=FIELDS) -> str:
    """PostgreSQL's words of these fields of a table's payload together; for all three FIELDS, the
    expression migration 0038 indexes."""
    value = " || ' ' || ".join(f"coalesce({table}.payload ->> '{name}', '')" for name in fields)
    return VECTOR.format(value)


@event.listens_for(Engine, "connect")
def sqlite_metadata_normalizer(connection, _):
    if isinstance(connection, sqlite3.Connection):
        connection.create_function("legacy_metadata_normalize", 1, normalize_citation, deterministic=True)
        connection.create_function("catalogue_words_match", 2, has_every_word, deterministic=True)
