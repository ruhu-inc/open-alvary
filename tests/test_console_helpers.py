"""Console helpers that need no database: audit-log network prefixes, browser families, addresses."""

import pytest

from accounts.store import AccountError, canonical_email, mailbox
from api.console import client_family, network_prefix


def test_network_prefix_keeps_a_network_never_an_address():
    assert network_prefix("203.0.113.57") == "203.0.113.0/24"
    assert network_prefix("::ffff:203.0.113.57") == "203.0.113.0/24"
    assert network_prefix("2001:db8:1234:5678::1") == "2001:db8:1234::/48"
    assert network_prefix("testclient") is None and network_prefix(None) is None


def test_client_family_is_coarse():
    chrome = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    assert client_family(chrome) == "Chrome"
    assert client_family(chrome + " Edg/129.0") == "Edge"
    assert (
        client_family("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15") == "Safari"
    )
    assert client_family("curl/8.7.1") == "curl" and client_family("") == "Other"


def test_an_address_keeps_its_local_part_and_lower_cases_its_domain():
    assert canonical_email("  Amina.U+law@EXAMPLE.org ") == "Amina.U+law@example.org"
    assert canonical_email("o'brien@bücher.example") == "o'brien@xn--bcher-kva.example"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "no-at-sign",
        "@example.org",
        "local@",
        "a@b@example.org",
        "two words@example.org",
        "a@example.org,b@example.org",
        "a;b@example.org",
        "<a>@example.org",
        '"quoted"@example.org',
        "new\nline@example.org",
        "bidi\u202eoverride@example.org",
        "x" * 65 + "@example.org",
        "a@nodot",
        "a@exa mple.org",
        "a@.example.org",
        "a@example..org",
    ],
)
def test_anything_but_one_plain_address_is_refused(value):
    with pytest.raises(AccountError) as refused:
        canonical_email(value)
    assert refused.value.code == "invalid_input"


def test_a_mailbox_is_the_same_however_the_address_is_written():
    assert mailbox("Guest@Example.org") == mailbox("guest+anything@example.org") == "guest@example.org"
    assert mailbox("g.u.e.s.t@gmail.com") == mailbox("GUEST+x@googlemail.com") == "guest@gmail.com"
    assert mailbox("g.uest@example.org") != mailbox("guest@example.org")  # dots count elsewhere
