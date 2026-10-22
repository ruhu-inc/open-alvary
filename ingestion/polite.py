"""Polite requests to official sites: who we are, robots.txt, the wait between requests to one host,
the hosts a source may lead to, and stopping at a refusal or a bot check.

Every request names the client honestly (filelist.user_agent). Before the first request to a host its
robots.txt is read (RFC 9309: a missing file, or any 4xx answer, allows everything; a server error or
no answer is read as "disallow everything", so the host is left alone), and a path it disallows for
us is refused before any request is made. A request to a host starts at least `delay` seconds after
the last one to it ended (or the robots.txt Crawl-delay, if longer); redirects are followed one by
one and only onto the hosts allowed; a host's stated rate limit is kept (a window of so many
requests, and its x-ratelimit-remaining answer). A refusal (401, 403, 429), a bot check (a
Cloudflare or Radware challenge page) or a rate limit used up raises Refused: the caller stops that
source and records why, never as "nothing listed".
"""

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from ingestion.filelist import Refused, user_agent

BOT_NAME = "OpenAlvaryBot"
MAX_REDIRECTS = 5
LEAST_DELAY = 2.0
LONGEST_CRAWL_DELAY = 60.0  # a Crawl-delay above this is read as this (we wait, but not for hours)


class Disallowed(Exception):
    """robots.txt disallows this path for us: never requested."""


class OffHost(Exception):
    """The address, or a redirect, leads off the hosts this source may use."""


class Unreachable(Exception):
    """The host did not answer (timeout, connection failure, server error), twice."""


class TooLarge(Exception):
    """The file is larger than the limit set for it."""


class BotCheck(Refused):
    """A bot check (Cloudflare or Radware challenge) answered instead of the page."""


# --- robots.txt ------------------------------------------------------------------------------------


@dataclass
class Robots:
    """The rules of one host's robots.txt that apply to us (our own group, else the "*" group)."""

    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None
    disallow_all: bool = False
    note: str = ""

    @classmethod
    def parse(cls, text: str, agent: str = BOT_NAME) -> "Robots":
        groups: list[tuple[list[str], list[tuple[bool, str]], list[float]]] = []
        current = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, value = (part.strip() for part in line.split(":", 1))
            name = name.lower()
            if name == "user-agent":
                if current is None or not last_was_agent:
                    current = ([], [], [])
                    groups.append(current)
                current[0].append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue
            if name in ("allow", "disallow") and value:
                current[1].append((name == "allow", value))
            elif name == "crawl-delay":
                try:
                    current[2].append(float(value))
                except ValueError:
                    pass
        token = agent.lower()
        mine = [g for g in groups if any(a != "*" and a in token for a in g[0])]
        chosen = mine or [g for g in groups if "*" in g[0]]
        rules = [rule for g in chosen for rule in g[1]]
        delays = [d for g in chosen for d in g[2]]
        return cls(rules=rules, crawl_delay=max(delays) if delays else None)

    def allowed(self, path: str) -> bool:
        """The longest matching rule wins; on a tie, Allow; with no matching rule, allowed."""
        if self.disallow_all:
            return False
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if _pattern(pattern).match(path):
                if best is None or len(pattern) > best[0] or (len(pattern) == best[0] and allow):
                    best = (len(pattern), allow)
        return True if best is None else best[1]


def _pattern(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


# --- bot checks ------------------------------------------------------------------------------------

CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/",
    "<title>just a moment...</title>",
    "attention required! | cloudflare",
    "radware bot manager",
    "validate.perfdrive.com",
    "shieldsquare",
    "captcha.perfdrive",
)


def bot_check(status: int, headers, head: bytes) -> str | None:
    """Why this answer is a bot check or refusal, or None: a 401, 403 or 429, Cloudflare's
    cf-mitigated: challenge, or a challenge page's own markers in the first part of the body."""
    if status in (401, 403, 429):
        return f"HTTP {status}"
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return "Cloudflare challenge (cf-mitigated)"
    text = head[:65536].decode("utf-8", "replace").lower()
    for marker in CHALLENGE_MARKERS:
        if marker in text:
            return f"bot check page ({marker})"
    return None


# --- the client ------------------------------------------------------------------------------------


@dataclass
class Answer:
    """A response read whole (or its first `head` bytes, or up to its limit), with the redirects
    that led to it."""

    url: str
    final_url: str
    status: int
    headers: dict
    content: bytes
    redirects: list[str]
    truncated: bool = False

    @property
    def text(self) -> str:
        charset = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
        try:
            return self.content.decode(charset.group(1) if charset else "utf-8", "replace")
        except LookupError:
            return self.content.decode("utf-8", "replace")

    def json(self):
        import json

        return json.loads(self.content)


class PoliteClient:
    """GET from a set of allowed hosts, politely. `windows` caps the requests to a host within a
    window of seconds (a stated rate limit: the Supreme Court API's 60 a minute)."""

    def __init__(
        self,
        hosts,
        delay: float = LEAST_DELAY,
        timeout: float = 60.0,
        windows: dict[str, tuple[int, float]] | None = None,
        client=None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retry_wait: float = 30.0,
        log: Callable[[str], None] = lambda message: None,
        record: Callable[["Answer"], None] | None = None,
    ):
        import httpx

        if delay < LEAST_DELAY:
            raise ValueError(f"requests to one host are at least {LEAST_DELAY:.0f} seconds apart")
        self.hosts = {h.lower() for h in hosts}
        self.delay = delay
        self.windows = {h.lower(): w for h, w in (windows or {}).items()}
        self.client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": user_agent()}, follow_redirects=False
        )
        self.clock, self.sleep, self.retry_wait, self.log = clock, sleep, retry_wait, log
        self.robots: dict[str, Robots] = {}
        self.last: dict[str, float] = {}  # when the last request to each host ended
        self.recent: dict[str, deque] = {}  # when the requests in the current window started
        self.remaining: dict[str, int] = {}  # what the host last said is left of its rate limit
        self.requests: dict[str, int] = {}
        self.refused_paths: list[str] = []
        self.record = record  # called with every answer returned (a Recorder keeps them)

    def close(self) -> None:
        self.client.close()

    def robots_state(self) -> dict:
        """The robots.txt rules read for each host, as a Recorder keeps them."""
        return {
            host: {
                "rules": r.rules,
                "crawl_delay": r.crawl_delay,
                "disallow_all": r.disallow_all,
                "note": r.note,
            }
            for host, r in self.robots.items()
        }

    # politeness ---------------------------------------------------------------------------------

    def _wait(self, host: str) -> None:
        robots = self.robots.get(host)
        delay = max(self.delay, min(LONGEST_CRAWL_DELAY, (robots.crawl_delay or 0) if robots else 0))
        if host in self.last:
            left = delay - (self.clock() - self.last[host])
            if left > 0:
                self.sleep(left)
        window = self.windows.get(host)
        if window:
            count, seconds = window
            recent = self.recent.setdefault(host, deque())
            while recent and self.clock() - recent[0] >= seconds:
                recent.popleft()
            if len(recent) >= count:
                self.sleep(seconds - (self.clock() - recent[0]))
                recent.popleft()
            if self.remaining.get(host, count) <= 1:  # nearly used up: let the host's window pass
                self.log(f"{host}: its rate limit is nearly used; waiting {seconds:.0f} s")
                self.sleep(seconds)
                self.remaining.pop(host)

    def _started(self, host: str) -> None:
        self.requests[host] = self.requests.get(host, 0) + 1
        if host in self.windows:
            self.recent.setdefault(host, deque()).append(self.clock())

    def _robots(self, scheme: str, host: str) -> Robots:
        if host in self.robots:
            return self.robots[host]
        url = f"{scheme}://{host}/robots.txt"
        try:
            status, headers, body, _ = self._send(host, url, None, None)
        except Unreachable as error:
            robots = Robots(disallow_all=True, note=f"robots.txt unreachable ({error}): host left alone")
        else:
            if status == 429:
                raise Refused(f"HTTP 429 at {url}")
            if status >= 400:
                robots = Robots(note=f"robots.txt HTTP {status}: no restrictions")
            elif status != 200:
                robots = Robots(note=f"robots.txt HTTP {status}: no restrictions")
            elif "html" in headers.get("content-type", "").lower() or body.lstrip()[:1] == b"<":
                robots = Robots(note="robots.txt answered with a web page: no restrictions declared")
            else:
                robots = Robots.parse(body.decode("utf-8", "replace"))
                robots.note = "robots.txt read"
        self.robots[host] = robots
        self.log(f"{host}: {robots.note}")
        return robots

    def permits(self, url: str) -> bool:
        """Whether robots.txt lets us have this address (its host's robots.txt read if not yet): a
        question, not a request, so it is not counted among the refused paths."""
        try:
            self.check(url, note=False)
        except (Disallowed, OffHost):
            return False
        return True

    def check(self, url: str, hosts=None, note: bool = True) -> None:
        """Refuse an address off the allowed hosts (all of this client's, or the narrower `hosts` of
        one source) or disallowed by its host's robots.txt."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        allowed = self.hosts if hosts is None else {h.lower() for h in hosts} & self.hosts
        if parts.scheme not in ("https", "http") or host not in allowed:
            raise OffHost(f"{url} is not on an allowed host")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if not self._robots(parts.scheme, host).allowed(path):
            if note:
                self.refused_paths.append(url)
            raise Disallowed(f"robots.txt of {host} disallows {path}")

    # requests -----------------------------------------------------------------------------------

    def _send(self, host: str, url: str, limit: int | None, head: int | None, headers: dict | None = None):
        """One GET, waited for, tried twice when the host does not answer (a timeout, a failed
        connection, a server error); the body read up to `limit` bytes (TooLarge past it), or only
        its first `head` bytes."""
        import httpx

        for attempt in (1, 2):
            self._wait(host)
            try:
                self._started(host)
                sent = {"User-Agent": user_agent(), **(headers or {})}  # who we are, on every request
                with self.client.stream("GET", url, headers=sent) as response:
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    if response.status_code >= 500:
                        reason = bot_check(response.status_code, headers, response.read())
                        if reason:  # Cloudflare's "under attack" check answers 503
                            raise BotCheck(f"{reason} at {url}")
                        raise _ServerError(response.status_code)
                    body, truncated = bytearray(), False
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if head is not None and len(body) >= head:
                            truncated = True
                            del body[head:]
                            break
                        if limit is not None and len(body) > limit:
                            raise TooLarge(f"{url} is larger than {limit} bytes")
                    return response.status_code, headers, bytes(body), truncated
            except (httpx.TransportError, _ServerError) as error:
                if attempt == 2:
                    raise Unreachable(f"{url}: {type(error).__name__}: {error}") from error
                self.log(f"{host}: {type(error).__name__} {error}; trying again in {self.retry_wait:.0f} s")
                self.last[host] = self.clock()
                self.sleep(self.retry_wait)
            finally:
                self.last[host] = self.clock()
        raise RuntimeError("unreachable")

    def get(
        self,
        url: str,
        limit: int | None = None,
        head: int | None = None,
        hosts=None,
        headers: dict | None = None,
    ) -> Answer:
        """GET an address on the allowed hosts (or the narrower `hosts` given); redirects followed one
        by one, each checked; `headers` sent with it (a conditional request's validators). Raises
        Refused on a refusal or bot check, Unreachable when the host does not answer, and returns any
        other answer (a 404 or a 304 included) for the caller to judge."""
        redirects: list[str] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.check(current, hosts)
            host = urlsplit(current).hostname.lower()
            status, answered, body, truncated = self._send(host, current, limit, head, headers)
            remaining = (answered.get("x-ratelimit-remaining") or "").strip()
            if remaining.isdigit():
                self.remaining[host] = int(remaining)
                if int(remaining) <= 0:
                    raise Refused(f"the rate limit of {host} is used up (x-ratelimit-remaining: 0)")
            if status in (301, 302, 303, 307, 308) and answered.get("location"):
                redirects.append(current)
                current = urljoin(current, answered["location"])
                continue
            reason = bot_check(status, answered, body)
            if reason:
                raise BotCheck(f"{reason} at {current}")
            answer = Answer(url, current, status, answered, body, redirects, truncated)
            if self.record:
                self.record(answer)
            return answer
        raise OffHost(f"more than {MAX_REDIRECTS} redirects from {url}")


class _ServerError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# --- keeping what a listing answered, and reading it again -------------------------------------


class Recorder:
    """Every answer a listing run reads, kept in its run folder: responses.jsonl.gz (each answer with
    its body) and index.json (the same without bodies, with the robots.txt rules read and the run's
    own details), so that the listing can be read again by better readers without the site."""

    KEPT_HEADERS = (
        "content-type",
        "content-length",
        "etag",
        "last-modified",
        "date",
        "x-wp-total",
        "x-wp-totalpages",
    )

    def __init__(self, folder):
        import gzip
        from pathlib import Path

        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.entries: list[dict] = []
        self.stream = gzip.open(self.folder / "responses.jsonl.gz", "wt", encoding="utf-8")

    def __call__(self, answer: Answer) -> None:
        import base64
        import hashlib
        import json
        from datetime import UTC, datetime

        meta = {
            "n": len(self.entries) + 1,
            "url": answer.url,
            "final_url": answer.final_url,
            "redirects": answer.redirects,
            "status": answer.status,
            "headers": {k: v for k, v in answer.headers.items() if k in self.KEPT_HEADERS},
            "truncated": answer.truncated,
            "bytes": len(answer.content),
            "sha256": hashlib.sha256(answer.content).hexdigest(),
            "read_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        self.entries.append(meta)
        self.stream.write(json.dumps({**meta, "body": base64.b64encode(answer.content).decode()}) + "\n")

    def close(self, details: dict) -> None:
        import json

        self.stream.close()
        index = {**details, "responses": self.entries}
        (self.folder / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n")


class ReplayClient:
    """A listing run's kept answers served again, in place of the site: a reader given one reads
    exactly what the run read, and makes no request. An address the run did not read (a reader that
    now asks for more) is answered as Unreachable, and robots.txt is as the run read it."""

    def __init__(self, folder, log: Callable[[str], None] = lambda message: None):
        import base64
        import gzip
        import json
        from pathlib import Path

        folder = Path(folder)
        self.index = json.loads((folder / "index.json").read_text())
        self.robots = {
            host: Robots(
                rules=[(bool(a), p) for a, p in v.get("rules", [])],
                crawl_delay=v.get("crawl_delay"),
                disallow_all=v.get("disallow_all", False),
                note=v.get("note", ""),
            )
            for host, v in (self.index.get("robots") or {}).items()
        }
        self.answers: dict[str, deque] = {}
        with gzip.open(folder / "responses.jsonl.gz", "rt", encoding="utf-8") as stream:
            for line in stream:
                e = json.loads(line)
                answer = Answer(
                    e["url"],
                    e["final_url"],
                    e["status"],
                    e["headers"],
                    base64.b64decode(e["body"]),
                    e.get("redirects") or [],
                    e.get("truncated", False),
                )
                self.answers.setdefault(e["url"], deque()).append(answer)
        self.requests: dict[str, int] = {}
        self.refused_paths: list[str] = []
        self.log = log

    def get(self, url: str, limit=None, head=None, hosts=None, headers=None) -> Answer:
        queue = self.answers.get(url)
        if not queue:
            raise Unreachable(f"{url} is not among the kept answers of this run")
        answer = queue.popleft() if len(queue) > 1 else queue[0]
        host = (urlsplit(url).hostname or "").lower()
        self.requests[host] = self.requests.get(host, 0) + 1
        return answer

    def permits(self, url: str) -> bool:
        parts = urlsplit(url)
        robots = self.robots.get((parts.hostname or "").lower())
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return bool(robots and robots.allowed(path))

    def robots_state(self) -> dict:
        return self.index.get("robots") or {}

    def close(self) -> None:
        pass
