"""Console helpers that need no database: audit-log network prefixes and browser families."""

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
