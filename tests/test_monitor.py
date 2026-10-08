"""Uptime probe: identity and readiness, with safe failure detail."""

import httpx
import pytest

from scripts.monitor_probe import probe


def transport(home=200, body="<title>Open Alvary</title>", ready=(200, {"status": "ok"})):
    def handler(request):
        if request.url.path == "/":
            return httpx.Response(home, text=body)
        return httpx.Response(ready[0], json=ready[1])

    return httpx.MockTransport(handler)


def test_healthy_site_passes():
    assert probe("https://example.test", 5, transport())[0]


@pytest.mark.parametrize(
    "kwargs,detail",
    [
        (dict(home=503), "homepage status 503"),
        (dict(body="Something else"), "homepage status 200"),
        (dict(ready=(503, {"status": "unavailable"})), "readiness status 503"),
    ],
)
def test_failures_report_only_safe_status(kwargs, detail):
    healthy, message = probe("https://example.test", 5, transport(**kwargs))
    assert not healthy and message == detail


def test_unreachable_site_fails_without_raising():
    def handler(request):
        raise httpx.ConnectError("refused")

    healthy, message = probe("https://example.test", 5, httpx.MockTransport(handler))
    assert not healthy and message.startswith("unreachable")
