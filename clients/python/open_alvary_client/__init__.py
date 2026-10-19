"""Reference Python client for Open Alvary's retrieval service, implementing the client contract
RSV-09: evidence and packs cached for the client's own users, kept honest by the change feed, the
check operation before reuse, labelled answers from the cache when Open Alvary cannot be reached, and
bounded retries. Reference code, not an official SDK."""

from .cache import Cache, MemoryCache, SQLiteCache
from .client import API, Answer, Cited, Client, SavedAnswer, SyncResult, SystemClock
from .errors import CacheExpired, OpenAlvaryError, RateLimited, Refused, Unavailable

__all__ = [
    "API",
    "Answer",
    "Cache",
    "CacheExpired",
    "Cited",
    "Client",
    "MemoryCache",
    "OpenAlvaryError",
    "RateLimited",
    "Refused",
    "SQLiteCache",
    "SavedAnswer",
    "SyncResult",
    "SystemClock",
    "Unavailable",
]
