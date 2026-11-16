"""Change types and cursors as callers meet them. Every list of change types a person sees names each type
the feed publishes, so a new type cannot be left showing as a raw code or missing from a monitor's
choices (consolidated findings, item 19); a cursor of any JSON type is refused as invalid (item 11)."""

import re
from pathlib import Path

import pytest

from accounts import mail
from changes import feed

ROOT = Path(__file__).resolve().parents[1]


def listed(path: str) -> list[str]:
    """The change types in a page's TYPES list, in its order, leaving out the "all" choice."""
    source = (ROOT / path).read_text()
    block = re.search(r"const TYPES: \[string, string\]\[\] = \[(.*?)\n\];", source, re.S)
    assert block, f"{path} has no TYPES list"
    return [t for t in re.findall(r'\[\s*"([^"]*)",\s*"[^"]+"\s*\]', block.group(1)) if t]


@pytest.mark.parametrize("path", ["web/src/changes.tsx", "web/src/console/watches.tsx"])
def test_each_page_offers_every_type_the_feed_publishes(path):
    assert sorted(listed(path)) == sorted(feed.SUPPORTED)


@pytest.mark.parametrize("labels", [feed.LABELS, mail.LABELS], ids=["feeds", "digests"])
def test_feeds_and_digests_name_every_type_the_feed_publishes(labels):
    assert sorted(labels) == sorted(feed.SUPPORTED)


@pytest.mark.parametrize("token", [5, None, ["c1.x"], {"c": 1}, "c1.!!", "x" * 3000])
def test_a_cursor_that_is_not_one_is_refused_as_invalid_whatever_its_type(token):
    """An MCP caller can send any JSON value as the cursor (consolidated findings, item 11)."""
    tokens = feed.Tokens({"k": bytes(32)}, "k")
    with pytest.raises(feed.FeedError) as refused:
        tokens.open("c", token)
    assert refused.value.code == "invalid_cursor"
