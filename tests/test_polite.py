"""Polite requests: who we are, robots.txt, the wait between requests to a host, the hosts allowed,
stated rate limits, and stopping at a refusal or a bot check; kept answers read again."""

import httpx
import pytest

from ingestion import polite


class Clock:
    """A clock the client's waits move on, so that spacing is checked without waiting."""

    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def client(routes: dict, clock=None, **options) -> tuple[polite.PoliteClient, list]:
    """A client over a fake site: routes maps an address to (status, headers, body) or a callable."""
    seen: list[tuple[float, str]] = []
    clock = clock or Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append((clock(), url, dict(request.headers)))
        answer = routes.get(url) or routes.get(url.split("?", 1)[0]) or (404, {}, b"")
        if callable(answer):
            answer = answer(request)
        status, headers, body = answer
        clock.t += 0.5  # the site takes half a second to answer
        return httpx.Response(status, headers=headers, content=body)

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    hosts = options.pop("hosts", {"example.gov.ng"})
    made = polite.PoliteClient(hosts, client=http, clock=clock, sleep=clock.sleep, retry_wait=30, **options)
    return made, seen


ROBOTS = "https://example.gov.ng/robots.txt"


def test_requests_to_one_host_are_two_seconds_apart_and_other_hosts_do_not_wait():
    routes = {ROBOTS: (404, {}, b""), "https://other.gov.ng/robots.txt": (404, {}, b"")}
    for n in range(3):
        routes[f"https://example.gov.ng/page/{n}"] = (200, {"content-type": "text/html"}, b"<p>listing</p>")
    routes["https://other.gov.ng/x"] = (200, {}, b"<p>other</p>")
    made, seen = client(routes, hosts={"example.gov.ng", "other.gov.ng"})
    for n in range(3):
        made.get(f"https://example.gov.ng/page/{n}")
    made.get("https://other.gov.ng/x")
    starts = [(t, url) for t, url, _ in seen if "example.gov.ng" in url]
    ends = [t + 0.5 for t, _ in starts]
    for (start, _), end in zip(starts[1:], ends, strict=False):
        assert start - end >= 2.0  # each request starts two seconds after the last one ended
    other = [t for t, url, _ in seen if "other.gov.ng" in url][0]  # its robots.txt, asked first
    assert other - ends[-1] < 2.0  # another host is not held up by this one's wait
    assert all(h["user-agent"].startswith("OpenAlvaryBot/") for _, _, h in seen)


def test_a_delay_under_two_seconds_is_refused_and_a_longer_crawl_delay_is_kept():
    with pytest.raises(ValueError):
        polite.PoliteClient({"example.gov.ng"}, delay=1.0)
    routes = {
        ROBOTS: (200, {"content-type": "text/plain"}, b"User-agent: *\nCrawl-delay: 5\n"),
        "https://example.gov.ng/a": (200, {}, b"a"),
        "https://example.gov.ng/b": (200, {}, b"b"),
    }
    made, seen = client(routes)
    made.get("https://example.gov.ng/a")
    made.get("https://example.gov.ng/b")
    times = [t for t, url, _ in seen if url.endswith(("/a", "/b"))]
    assert times[1] - (times[0] + 0.5) >= 5.0


def test_robots_txt_is_read_once_and_a_disallowed_path_is_never_requested():
    rules = b"User-agent: *\nDisallow: /search\nDisallow: /browse/*\nAllow: /browse/ok$\n"
    routes = {
        ROBOTS: (200, {"content-type": "text/plain"}, rules),
        "https://example.gov.ng/items/1": (200, {}, b"x"),
    }
    made, seen = client(routes)
    made.get("https://example.gov.ng/items/1")
    for path in ("/search?q=act", "/browse/title"):
        with pytest.raises(polite.Disallowed):
            made.get(f"https://example.gov.ng{path}")
    assert made.permits("https://example.gov.ng/browse/ok")
    assert not made.permits("https://example.gov.ng/browse/ok/more")
    assert [url for _, url, _ in seen] == [ROBOTS, "https://example.gov.ng/items/1"]
    assert made.refused_paths == [
        "https://example.gov.ng/search?q=act",
        "https://example.gov.ng/browse/title",
    ]


def test_robots_txt_absent_allows_everything_and_a_server_error_leaves_the_host_alone():
    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/a": (200, {}, b"a")}
    made, _ = client(routes)
    assert made.get("https://example.gov.ng/a").status == 200
    routes = {ROBOTS: (503, {}, b"down"), "https://example.gov.ng/a": (200, {}, b"a")}
    made, seen = client(routes)
    with pytest.raises(polite.Disallowed):
        made.get("https://example.gov.ng/a")
    assert all(url == ROBOTS for _, url, _ in seen)  # asked twice (a second try), then left alone
    routes = {
        ROBOTS: (200, {"content-type": "text/html"}, b"<html>home page</html>"),
        "https://example.gov.ng/a": (200, {}, b"a"),
    }
    made, _ = client(routes)
    assert made.get("https://example.gov.ng/a").status == 200  # a home page for robots.txt: no rules


def test_our_own_group_in_robots_txt_is_the_one_kept():
    robots = polite.Robots.parse(
        "User-agent: *\nDisallow: /\n\nUser-agent: OpenAlvaryBot\nDisallow: /private/\n"
    )
    assert robots.allowed("/laws/") and not robots.allowed("/private/x")
    google = polite.Robots.parse("User-agent: *\nAllow: /file\nAllow: /$\nDisallow: /\n")
    assert google.allowed("/file/d/abc/view") and not google.allowed("/uc?export=download&id=abc")


@pytest.mark.parametrize(
    "answer",
    [
        (403, {}, b"Forbidden"),
        (429, {"retry-after": "60"}, b""),
        (200, {"cf-mitigated": "challenge"}, b"<html></html>"),
        (200, {"content-type": "text/html"}, b"<html><head><title>Just a moment...</title></head></html>"),
        (
            503,
            {"server": "cloudflare"},
            b"<script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script>",
        ),
        (200, {}, b"<html>Radware Bot Manager Captcha</html>"),
    ],
)
def test_a_refusal_or_a_bot_check_page_raises_refused(answer):
    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/feed": answer}
    made, _ = client(routes)
    with pytest.raises(polite.Refused):
        made.get("https://example.gov.ng/feed")


def test_a_host_that_does_not_answer_is_tried_once_more_then_unreachable():
    calls = []

    def down(request):
        calls.append(1)
        raise httpx.ConnectTimeout("timed out", request=request)

    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/feed": down}
    clock = Clock()
    made, _ = client(routes, clock=clock)
    with pytest.raises(polite.Unreachable):
        made.get("https://example.gov.ng/feed")
    assert len(calls) == 2 and 30 in clock.slept


def test_redirects_are_followed_only_onto_the_allowed_hosts():
    routes = {
        ROBOTS: (404, {}, b""),
        "https://example.gov.ng/old": (301, {"location": "https://example.gov.ng/new"}, b""),
        "https://example.gov.ng/new": (200, {}, b"moved"),
        "https://example.gov.ng/away": (302, {"location": "https://spam.example.com/x"}, b""),
    }
    made, _ = client(routes)
    answer = made.get("https://example.gov.ng/old")
    assert answer.final_url == "https://example.gov.ng/new" and answer.redirects == [
        "https://example.gov.ng/old"
    ]
    with pytest.raises(polite.OffHost):
        made.get("https://example.gov.ng/away")
    with pytest.raises(polite.OffHost):
        made.get("https://example.gov.ng/new", hosts={"elsewhere.gov.ng"})  # narrower hosts for one source


def test_a_stated_rate_limit_is_kept_and_a_used_up_one_stops():
    remaining = iter(["3", "2", "1", "40"])

    def limited(request):
        return (200, {"x-ratelimit-limit": "60", "x-ratelimit-remaining": next(remaining)}, b"{}")

    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/api": limited}
    clock = Clock()
    made, _ = client(routes, clock=clock, windows={"example.gov.ng": (60, 60.0)})
    for _ in range(4):
        made.get("https://example.gov.ng/api")
    assert 60.0 in clock.slept  # nearly used up: the host's window is let pass before the next
    routes["https://example.gov.ng/api"] = (200, {"x-ratelimit-remaining": "0"}, b"{}")
    with pytest.raises(polite.Refused, match="used up"):
        made.get("https://example.gov.ng/api")


def test_a_window_of_requests_is_never_exceeded():
    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/api": (200, {}, b"{}")}
    clock = Clock()
    made, seen = client(routes, clock=clock, windows={"example.gov.ng": (3, 60.0)})
    for _ in range(5):
        made.get("https://example.gov.ng/api")
    starts = [t for t, url, _ in seen if url.endswith("/api")]
    assert starts[3] - starts[0] >= 60.0  # the fourth waits until the first has left the window


def test_head_reads_only_the_first_bytes_and_limit_refuses_a_larger_file():
    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/page": (200, {}, b"x" * 100_000)}
    made, _ = client(routes)
    answer = made.get("https://example.gov.ng/page", head=1000)
    assert answer.truncated and 1000 <= len(answer.content) < 100_000
    with pytest.raises(polite.TooLarge):
        made.get("https://example.gov.ng/page", limit=50_000)


def test_conditional_headers_are_sent_and_a_304_is_returned_for_the_caller():
    routes = {ROBOTS: (404, {}, b""), "https://example.gov.ng/file.pdf": (304, {"etag": '"v1"'}, b"")}
    made, seen = client(routes)
    answer = made.get("https://example.gov.ng/file.pdf", headers={"If-None-Match": '"v1"'})
    assert answer.status == 304 and seen[-1][2]["if-none-match"] == '"v1"'


def test_kept_answers_are_served_again_without_the_site(tmp_path):
    routes = {
        ROBOTS: (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private\n"),
        "https://example.gov.ng/feed?page=1": (
            200,
            {"content-type": "application/json", "etag": '"a"'},
            b'{"rows": [1]}',
        ),
    }
    recorder = polite.Recorder(tmp_path / "run")
    made, _ = client(routes, record=recorder)
    made.get("https://example.gov.ng/feed?page=1")
    recorder.close({"run": "r1", "robots": made.robots_state(), "state_before": {}})
    replay = polite.ReplayClient(tmp_path / "run")
    answer = replay.get("https://example.gov.ng/feed?page=1")
    assert answer.json() == {"rows": [1]} and answer.headers["etag"] == '"a"'
    assert not replay.permits("https://example.gov.ng/private/x") and replay.permits(
        "https://example.gov.ng/feed"
    )
    with pytest.raises(polite.Unreachable):
        replay.get("https://example.gov.ng/feed?page=2")  # never read: not invented


def test_robots_txt_follows_its_redirects_reads_a_bounded_part_ignores_a_blank_agent_and_knows_ports():
    """Consolidated findings item 47."""
    rules = b"User-agent: *\nDisallow: /private/\n"
    moved = "https://example.gov.ng/static/robots.txt"
    made, seen = client(
        {
            ROBOTS: (301, {"location": "/static/robots.txt"}, b""),
            moved: (200, {"content-type": "text/plain"}, rules),
            "https://example.gov.ng/a": (200, {}, b"a"),
        }
    )
    assert made.get("https://example.gov.ng/a").status == 200
    with pytest.raises(polite.Disallowed):  # the rules where it was redirected apply here
        made.get("https://example.gov.ng/private/x")
    loop = {ROBOTS: (302, {"location": ROBOTS}, b""), "https://example.gov.ng/a": (200, {}, b"a")}
    made, _ = client(loop)
    with pytest.raises(polite.Disallowed):  # redirected for ever: the host is left alone
        made.get("https://example.gov.ng/a")
    # A robots.txt past 500 KiB is read only that far, not refused or read whole.
    big = b"User-agent: *\nDisallow: /private/\n" + b"#" * (2 * polite.ROBOTS_BYTES)
    made, _ = client({ROBOTS: (200, {"content-type": "text/plain"}, big)})
    assert not made.permits("https://example.gov.ng/private/x")
    # A blank User-agent line names nobody: the group under it is no rule of ours.
    blank = polite.Robots.parse("User-agent:\nDisallow: /\n\nUser-agent: *\nDisallow: /private/\n")
    assert blank.allowed("/laws/") and not blank.allowed("/private/x")
    # Another port is another origin, with its own robots.txt.
    made, seen = client(
        {
            ROBOTS: (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /\n"),
            "https://example.gov.ng:8443/robots.txt": (404, {}, b""),
            "https://example.gov.ng:8443/a": (200, {}, b"a"),
        }
    )
    assert made.get("https://example.gov.ng:8443/a").status == 200
    assert not made.permits("https://example.gov.ng/a")


def test_the_file_list_fetcher_obeys_robots_txt_and_reads_a_bounded_page():
    from ingestion import filelist

    made, seen = client(
        {
            ROBOTS: (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n"),
            "https://example.gov.ng/files/": (200, {}, b"<html>listing</html>"),
            "https://example.gov.ng/huge/": (200, {}, b"x" * (filelist.LISTING_BYTES + 1)),
        }
    )
    fetch = filelist.fetcher("https://example.gov.ng", client=made)
    assert fetch("https://example.gov.ng/files/") == "<html>listing</html>"
    for refused in ("https://example.gov.ng/private/list", "https://example.gov.ng/huge/"):
        with pytest.raises(filelist.Refused):
            fetch(refused)
    assert "https://example.gov.ng/private/list" not in [url for _, url, _ in seen]


def test_a_second_try_sends_what_the_first_did_and_never_the_answers_headers():
    """Findings, 2026-11-19: the retry after a server error sent the error's own headers (its
    content-length on a GET with no body) and lost the caller's conditional headers."""
    tries = []

    def flaky(request):
        tries.append(dict(request.headers))
        if len(tries) == 1:
            return 503, {"set-cookie": "s=1", "x-backend": "b1", "content-type": "text/plain"}, b"x" * 10
        return 200, {}, b"ok"

    made, _ = client({ROBOTS: (404, {}, b""), "https://example.gov.ng/doc": flaky})
    answer = made.get("https://example.gov.ng/doc", headers={"If-None-Match": '"abc"'})
    assert answer.status == 200 and len(tries) == 2
    first, second = tries
    assert second["if-none-match"] == first["if-none-match"] == '"abc"'
    assert second["user-agent"] == first["user-agent"]
    assert not {"set-cookie", "x-backend", "content-type", "content-length"} & set(second)


def test_only_the_first_part_of_a_server_errors_body_is_read():
    read = []

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                read.append(1)
                yield b"x" * 4096

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503, stream=Endless())

    clock = Clock()
    made = polite.PoliteClient(
        {"example.gov.ng"},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        sleep=clock.sleep,
    )
    with pytest.raises(polite.Unreachable):
        made.get("https://example.gov.ng/doc")
    assert len(read) <= 2 * (polite.BOT_CHECK_BYTES // 4096 + 1)  # two tries, 64 KiB each at most
    # A bot check page that answers 503 is still recognised from that first part.
    page = b"<html><title>Just a moment...</title>" + b" " * (2 * polite.BOT_CHECK_BYTES)
    made, _ = client({ROBOTS: (404, {}, b""), "https://example.gov.ng/doc": (503, {}, page)})
    with pytest.raises(polite.BotCheck):
        made.get("https://example.gov.ng/doc")


@pytest.mark.parametrize(
    "target, addresses",
    [
        ("http://169.254.169.254/computeMetadata/v1/", []),  # a cloud metadata server
        ("http://127.0.0.1/robots.txt", []),
        ("http://[::1]/robots.txt", []),
        ("http://10.0.0.5/robots.txt", []),
        ("https://internal.example/robots.txt", ["10.1.2.3"]),  # a name for a private address
        ("https://mixed.example/robots.txt", ["93.184.215.14", "192.168.1.1"]),
        ("https://nowhere.example/robots.txt", []),  # a name that does not resolve
    ],
)
def test_a_robots_txt_redirected_off_the_public_internet_is_never_followed(target, addresses):
    """Findings, 2026-11-19: robots.txt redirects were followed to any address."""
    made, seen = client(
        {ROBOTS: (301, {"location": target}, b""), "https://example.gov.ng/a": (200, {}, b"a")},
        resolver=lambda host: addresses,
    )
    with pytest.raises(polite.Disallowed):  # left alone, as for an unreachable robots.txt
        made.get("https://example.gov.ng/a")
    assert [url for _, url, _ in seen] == [ROBOTS]
    assert "public internet" in made.robots["example.gov.ng"].note


def test_a_robots_txt_redirected_to_a_public_host_or_an_allowed_one_is_read_there():
    """RFC 9309, 2.3.1.2: the rules where robots.txt was redirected apply, even on another host."""
    rules = (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n")
    for target, hosts in (
        ("https://cdn.example.net/robots.txt", {"example.gov.ng"}),
        ("https://www.example.gov.ng/robots.txt", {"example.gov.ng", "www.example.gov.ng"}),
    ):
        asked = []
        made, _ = client(
            {ROBOTS: (301, {"location": target}, b""), target: rules},
            hosts=hosts,
            resolver=lambda host, asked=asked: asked.append(host) or ["93.184.215.14"],
        )
        assert not made.permits("https://example.gov.ng/private/x")
        assert made.permits("https://example.gov.ng/laws/")
        assert asked == ([] if "www." in target else ["cdn.example.net"])  # an allowed host is not resolved


def test_a_public_host_is_one_whose_every_address_is_global():
    assert polite.public_host("93.184.215.14")
    assert polite.public_host(
        "example.org", lambda host: ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]
    )
    assert not polite.public_host("100.64.0.1")  # shared address space (carrier-grade NAT)
    assert not polite.public_host("fe80::1")
    assert not polite.public_host("example.org", lambda host: ["not an address"])
