"""Exact metadata normalization; PostgreSQL uses an indexed ASCII fast path."""

import sqlite3
import unicodedata

from sqlalchemy import event
from sqlalchemy.engine import Engine


def normalize_citation(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@event.listens_for(Engine, "connect")
def sqlite_metadata_normalizer(connection, _):
    if isinstance(connection, sqlite3.Connection):
        connection.create_function("legacy_metadata_normalize", 1, normalize_citation, deterministic=True)


def literal_pattern(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
