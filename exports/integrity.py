"""Whether a release's rows form a valid Corpus, worked out a row at a time.

Corpus.model_validate needs every row at once, as models, texts and all. This validates each row as its own
model, keeps of it only what other rows refer to, in a SQLite database in a temporary folder, and asks the
references of that: each of Corpus.integrity's rules is here as the rows that would break it. A text is held
only while its own nodes are checked against it. The answer is the same for the same rows, and
tests/test_release_integrity.py compares the two on valid releases and broken ones; a rule added to
Corpus.integrity fails that test until it is added here too.

What memory it takes no longer grows with the release, but for the scratch database's file, which holds a few
short values for each row. Where the temporary folder is itself held in memory, as on Cloud Run, that file is
what a large release costs.
"""

import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from hashlib import sha256
from itertools import batched
from pathlib import Path
from typing import get_args

from schema.models import Corpus

# Each file's model, and of each row what other rows refer to.
MODELS = {name: get_args(field.annotation)[0] for name, field in Corpus.model_fields.items()}
KEPT = {
    "languages": ("id",),
    "jurisdictions": ("id", "parent_id"),
    "authorities": ("id", "jurisdiction_id"),
    "sources": ("id", "jurisdiction_id", "authority_id", "language"),
    "rights": ("id", "source_id"),
    "structure": (
        "id",
        "source_id",
        "version_id",
        "source_hash",
        "parent_id",
        "target_node_id",
        "start_byte",
        "end_byte",
        "span_hash",
    ),
    # Read after the structure, so that each text is checked against its nodes as it goes by.
    "versions": ("id", "source_id", "language", "content_hash", "prior_version_id"),
    "citations": ("id", "source_id", "version_id", "node_id"),
    "amendments": ("id", "source_id", "target_source_id"),
    "translations": ("id", "source_version_id", "translated_version_id", "language"),
}
TABLES = """
CREATE TABLE languages (id TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE jurisdictions (id TEXT PRIMARY KEY, parent_id TEXT) WITHOUT ROWID;
CREATE TABLE jurisdiction_languages (language TEXT NOT NULL);
CREATE TABLE authorities (id TEXT PRIMARY KEY, jurisdiction_id TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE sources (
    id TEXT PRIMARY KEY, jurisdiction_id TEXT NOT NULL, authority_id TEXT NOT NULL, language TEXT NOT NULL
) WITHOUT ROWID;
-- Only one active rights decision per source.
CREATE TABLE rights (id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE structure (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, version_id TEXT NOT NULL, source_hash BLOB NOT NULL,
    parent_id TEXT, target_node_id TEXT, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, span_hash BLOB
) WITHOUT ROWID;
CREATE TABLE versions (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, language TEXT NOT NULL, content_hash BLOB NOT NULL,
    prior_version_id TEXT
) WITHOUT ROWID;
CREATE TABLE citations (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, version_id TEXT, node_id TEXT) WITHOUT ROWID;
CREATE TABLE amendments (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_source_id TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE translations (
    id TEXT PRIMARY KEY, source_version_id TEXT NOT NULL, translated_version_id TEXT NOT NULL,
    language TEXT NOT NULL
) WITHOUT ROWID;
"""
# Made once the structure is in, before the texts are read: their nodes, and the walk from each node's parent.
INDEXES = (
    "CREATE INDEX structure_version ON structure (version_id)",
    "CREATE INDEX structure_parent ON structure (parent_id)",
)
NODES = "SELECT start_byte, end_byte, span_hash FROM structure WHERE version_id = ?"
# A hash is kept as its 32 bytes: each was validated as 64 lower-case hexadecimal digits, so nothing is lost.
HASHES = {"content_hash", "source_hash", "span_hash"}

# Corpus.integrity's rules in its order, each as a query for a row that breaks it (duplicate IDs, and a second
# rights decision for a source, are refused by the tables' keys as the rows go in).
BROKEN = (
    "SELECT 1 FROM jurisdictions WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM jurisdictions)",
    "SELECT 1 FROM jurisdiction_languages WHERE language NOT IN (SELECT id FROM languages)",
    "SELECT 1 FROM authorities WHERE jurisdiction_id NOT IN (SELECT id FROM jurisdictions)",
    "SELECT 1 FROM sources WHERE jurisdiction_id NOT IN (SELECT id FROM jurisdictions)"
    " OR authority_id NOT IN (SELECT id FROM authorities) OR language NOT IN (SELECT id FROM languages)",
    "SELECT 1 FROM rights WHERE source_id NOT IN (SELECT id FROM sources)",
    # A prior version must be another version of the same source.
    "SELECT 1 FROM versions v LEFT JOIN versions p ON p.id = v.prior_version_id"
    " WHERE v.source_id NOT IN (SELECT id FROM sources) OR v.language NOT IN (SELECT id FROM languages)"
    " OR (v.prior_version_id IS NOT NULL AND (p.id IS NULL OR p.id = v.id OR p.source_id <> v.source_id))",
    # A node names its version's source and text (its offsets are checked against the text as it is read).
    "SELECT 1 FROM structure n LEFT JOIN versions v ON v.id = n.version_id"
    " WHERE v.id IS NULL OR n.source_id <> v.source_id OR n.source_hash <> v.content_hash",
    # A contents entry points into its own version.
    "SELECT 1 FROM structure n LEFT JOIN structure t ON t.id = n.target_node_id"
    " WHERE n.target_node_id IS NOT NULL AND (t.id IS NULL OR t.version_id <> n.version_id)",
    # A parent is in the same version, around its child.
    "SELECT 1 FROM structure n LEFT JOIN structure p ON p.id = n.parent_id WHERE n.parent_id IS NOT NULL AND ("
    " p.id IS NULL OR p.version_id <> n.version_id"
    " OR NOT (p.start_byte <= n.start_byte AND n.start_byte <= n.end_byte AND n.end_byte <= p.end_byte))",
    # No cycle: with every parent there, each node is reached from one without a parent, and only once.
    "WITH RECURSIVE reached(id) AS (SELECT id FROM structure WHERE parent_id IS NULL"
    " UNION ALL SELECT s.id FROM structure s JOIN reached r ON s.parent_id = r.id)"
    " SELECT 1 WHERE (SELECT count(*) FROM reached) <> (SELECT count(*) FROM structure)",
    "SELECT 1 FROM citations c LEFT JOIN versions v ON v.id = c.version_id"
    " LEFT JOIN structure n ON n.id = c.node_id WHERE c.source_id NOT IN (SELECT id FROM sources)"
    " OR (c.version_id IS NOT NULL AND (v.id IS NULL OR v.source_id <> c.source_id))"
    " OR (c.node_id IS NOT NULL AND (n.id IS NULL OR n.source_id <> c.source_id OR n.version_id IS NOT c.version_id))",
    "SELECT 1 FROM amendments"
    " WHERE source_id NOT IN (SELECT id FROM sources) OR target_source_id NOT IN (SELECT id FROM sources)",
    "SELECT 1 FROM translations t LEFT JOIN versions o ON o.id = t.source_version_id"
    " LEFT JOIN versions d ON d.id = t.translated_version_id"
    " WHERE o.id IS NULL OR d.id IS NULL OR t.language NOT IN (SELECT id FROM languages)"
    " OR o.source_id <> d.source_id OR d.language <> t.language",
)

BATCH = 1000
# The largest offset SQLite holds. No text comes near it, so a node past it is outside its text, whatever text.
LARGEST_OFFSET = 2**63 - 1


class ScratchUnavailable(RuntimeError):
    """No temporary folder or scratch database to check a release in: no answer about the release, so it is never
    taken for an invalid one, nor remembered as one."""


def valid(rows: Callable[[str], Iterable[dict]]) -> bool:
    """Whether the rows of every file (``rows(name)``, for each of Corpus's fields) form a valid Corpus: what
    Corpus.model_validate would answer, given them all at once. A row that does not parse or validate is a no;
    a file that cannot be read raises as it did, and the scratch database's own failure is ScratchUnavailable."""
    try:
        scratch = tempfile.TemporaryDirectory(prefix="release-rows-", ignore_cleanup_errors=True)
    except OSError:
        raise ScratchUnavailable("No temporary folder to check a release in") from None
    with scratch as folder:
        try:
            db = sqlite3.connect(Path(folder) / "rows.db")
        except sqlite3.Error:
            raise ScratchUnavailable("No scratch database to check a release in") from None
        try:
            for pragma in (
                "journal_mode = OFF",
                "synchronous = OFF",
                "temp_store = FILE",
                "cache_size = -16384",
            ):
                db.execute(f"PRAGMA {pragma}")
            db.executescript(TABLES)
            return _load(db, rows) and not any(db.execute(query).fetchone() for query in BROKEN)
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError):
            # Not valid, as a row's model or Corpus.model_validate would say; an ID twice is IntegrityError here.
            return False
        except sqlite3.Error:
            raise ScratchUnavailable("The scratch database failed while checking a release") from None
        finally:
            db.close()


def _load(db: sqlite3.Connection, rows: Callable[[str], Iterable[dict]]) -> bool:
    for name, kept in KEPT.items():
        model = MODELS[name]
        if name == "versions":
            db.executescript(";".join(INDEXES))
            insert = _insert(name, kept)
            for row in rows(name):
                version = model.model_validate(row)
                db.execute(insert, _values(version, kept))
                if not _text_holds(version.canonical_text, db.execute(NODES, (version.id,))):
                    return False
            continue
        for batch in batched((model.model_validate(row) for row in rows(name)), BATCH):
            if name == "structure" and any(node.end_byte > LARGEST_OFFSET for node in batch):
                return False
            db.executemany(_insert(name, kept), (_values(item, kept) for item in batch))
            if name == "jurisdictions":
                db.executemany(
                    "INSERT INTO jurisdiction_languages VALUES (?)",
                    ((language,) for item in batch for language in item.languages),
                )
    return True


def _values(item, kept: tuple[str, ...]) -> list:
    values = (getattr(item, column) for column in kept)
    return [
        bytes.fromhex(v) if c in HASHES and v is not None else v for c, v in zip(kept, values, strict=True)
    ]


def _insert(name: str, kept: tuple[str, ...]) -> str:
    return f"INSERT INTO {name} ({', '.join(kept)}) VALUES ({', '.join('?' * len(kept))})"


def _text_holds(text: str, nodes: Iterable[tuple[int, int, bytes | None]]) -> bool:
    """Whether a text holds the nodes that name its version: each ends inside it, starts and ends between
    characters, and has the bytes its span hash says."""
    raw = None
    for start, end, span_hash in nodes:
        raw = text.encode() if raw is None else raw
        if end > len(raw) or not (_between_characters(raw, start) and _between_characters(raw, end)):
            return False
        if span_hash and sha256(memoryview(raw)[start:end]).digest() != span_hash:
            return False
    return True


def _between_characters(raw: bytes, offset: int) -> bool:
    """Whether the bytes up to the offset decode, as Corpus.integrity asks: the text is valid UTF-8 (its version
    validated), so only an offset that falls inside a character fails, where the next byte continues one."""
    return offset >= len(raw) or raw[offset] & 0xC0 != 0x80
