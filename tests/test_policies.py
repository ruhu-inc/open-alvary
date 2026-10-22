"""The documents people accept (D25): the website shows the version the console records."""

import re
from pathlib import Path

from accounts import policies


def test_the_website_shows_the_version_people_accept():
    source = Path("web/src/legal.tsx").read_text()
    shown = re.search(r'LEGAL_VERSION = "(\d{4}-\d{2}-\d{2})"', source).group(1)
    assert set(policies.CURRENT.values()) == {shown}
    for path in policies.PATHS.values():
        assert f'pathname === "{path}"' in Path("web/src/main.tsx").read_text()


def test_the_current_version_says_what_it_changes_for_the_owners_notice():
    assert set(policies.CURRENT.values()) <= set(policies.CHANGES)
