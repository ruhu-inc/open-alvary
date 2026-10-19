"""Runs the RSV-09 conformance scenarios against a client implementation.

Each scenario in ``scenarios/`` is JSON: a start time and steps. A step does one thing to the client
(``sync``, ``retrieve``, ``check``) or to time (``advance``). It lists, in order, the requests the
client must make during it (``exchanges``) with Open Alvary's answer to each, and says what the client
must give back (``expect``). An answer's body may name a shared body in ``bodies.json`` as ``"$name"``;
``"error": "connect"`` or ``"timeout"`` stands for Open Alvary out of reach. A request must match the
method and path, and the ``params``, ``headers`` and ``json`` given (a JSON object matches when every
key given matches); ``not_before`` is the least number of seconds since the previous request, which
is how Retry-After is checked. Any request beyond those listed fails the step.

A Python implementation runs them with ``run(scenario, make_client)``. ``make_client(base_url=, key=,
transport=, clock=)`` returns a client with ``retrieve(query, **filters)``, ``check(items)`` and
``sync_changes()``, which sends every request through ``transport`` (an ``httpx.BaseTransport``) and
reads time only from ``clock`` (``now()`` and ``sleep(seconds)``). An answer needs ``pack``,
``from_cache``, ``checked``, ``offline`` and ``units`` (each with ``unit``, ``label`` and
``cached_at``); an error is matched by class name. Throughout, no log record may contain a query or
any legal text from the answers.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx

HERE = Path(__file__).resolve().parent
BASE = "https://api.open.alvary.ai/api/v1"
KEY = "conformance-key"
ERRORS = {"connect": httpx.ConnectError, "timeout": httpx.ReadTimeout}


def scenarios() -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted((HERE / "scenarios").glob("*.json"))]


def bodies() -> dict:
    return json.loads((HERE / "bodies.json").read_text())


class FakeClock:
    """Time that moves only when a step advances it or the client sleeps."""

    def __init__(self, start: datetime):
        self.at = start

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


def _within(expected, actual) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _within(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list) and len(actual) == len(expected) and all(map(_within, expected, actual))
        )
    return expected == actual


class Server:
    """Open Alvary as a scenario scripts it: each request must be the next one listed."""

    def __init__(self, clock: FakeClock, shared: dict):
        self.clock, self.shared = clock, shared
        self.script: list[dict] = []
        self.problems: list[str] = []
        self.last: datetime | None = None

    def expect(self, exchanges: list[dict]) -> None:
        self.script = [e for e in exchanges for _ in range(e.get("repeat", 1))]
        self.problems = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(httpx.URL(BASE).path)
        shown = f"{request.method} {path}"
        since = None if self.last is None else (self.clock.now() - self.last).total_seconds()
        self.last = self.clock.now()
        if request.headers.get("authorization") != f"Bearer {KEY}":
            self.problems.append(f"{shown} was sent without the client's key")
        if not self.script:
            self.problems.append(f"unexpected request: {shown}")
            return httpx.Response(418, json={"code": "unexpected_request"})
        exchange = self.script.pop(0)
        if (mismatch := self._mismatch(exchange["request"], request, path, since)) is not None:
            self.problems.append(f"{shown}: {mismatch}")
        answer = exchange["response"]
        if "error" in answer:
            raise ERRORS[answer["error"]]("Open Alvary is out of reach (scripted).", request=request)
        body = answer.get("body")
        if isinstance(body, str) and body.startswith("$"):
            body = self.shared[body[1:]]
        headers = answer.get("headers", {})
        if body is None:
            return httpx.Response(answer.get("status", 200), headers=headers)
        return httpx.Response(answer.get("status", 200), json=body, headers=headers)

    @staticmethod
    def _mismatch(wanted: dict, request: httpx.Request, path: str, since: float | None) -> str | None:
        if (wanted["method"], wanted["path"]) != (request.method, path):
            return f"expected {wanted['method']} {wanted['path']}"
        for name, value in wanted.get("params", {}).items():
            if request.url.params.get_list(name) != (value if isinstance(value, list) else [value]):
                return f"parameter {name} is {request.url.params.get_list(name)}, expected {value}"
        for name, value in wanted.get("headers", {}).items():
            if request.headers.get(name) != value:
                return f"header {name} is {request.headers.get(name)!r}, expected {value!r}"
        if "json" in wanted and not _within(wanted["json"], json.loads(request.content or b"null")):
            return f"body {request.content.decode()} does not match {json.dumps(wanted['json'])}"
        if since is not None and since < wanted.get("not_before", 0):
            return f"sent {since:g} s after the previous request, expected at least {wanted['not_before']} s"
        return None


class _Logs(logging.Handler):
    """Every log record while a scenario runs, from any logger."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _secrets(scenario: dict, shared: dict) -> set[str]:
    """Query text and legal text, as written and as a URL carries them."""
    found = {step["query"] for step in scenario["steps"] if "query" in step}
    found |= {" ".join(q.split()) for q in found}

    def walk(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                if key == "text" and isinstance(inner, str):
                    found.add(inner)
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(shared)
    return found | {quote_plus(s) for s in found} | {quote(s) for s in found}


def _do(client, clock: FakeClock, step: dict):
    try:
        if step["do"] == "advance":
            clock.advance(step["seconds"])
            return None, None
        if step["do"] == "sync":
            return client.sync_changes(), None
        if step["do"] == "retrieve":
            return client.retrieve(step["query"], **step.get("filters", {})), None
        if step["do"] == "check":
            return client.check(step["items"]), None
    except Exception as error:  # the step says whether one was expected
        return None, error
    raise ValueError(f"Unknown step: {step['do']}")


def _answer(where: str, wanted: dict, answer) -> None:
    for name in ("from_cache", "checked", "offline"):
        if name in wanted:
            assert getattr(answer, name) == wanted[name], f"{where}: {name} is {getattr(answer, name)}"
    anchors = [u["anchor_id"] for u in answer.pack["evidence"]]
    if "evidence" in wanted:
        assert anchors == wanted["evidence"], f"{where}: evidence is {anchors}"
    labels = [u.label for u in answer.units]
    if "labels" in wanted:
        assert labels == [wanted["labels"]] * len(anchors), f"{where}: labels are {labels}"
    if "cached_at" in wanted:
        cached = [u.cached_at for u in answer.units]
        assert cached == [datetime.fromisoformat(wanted["cached_at"])] * len(anchors), (
            f"{where}: cached_at {cached}"
        )
    for anchor, state in wanted.get("currentness", {}).items():
        shown = next(u["currentness"]["state"] for u in answer.pack["evidence"] if u["anchor_id"] == anchor)
        assert shown == state, f"{where}: {anchor} is {shown}, expected {state}"


def run(scenario: dict, make_client) -> None:
    """Run one scenario; an AssertionError says which step failed and how."""
    clock = FakeClock(datetime.fromisoformat(scenario["start"]))
    shared = bodies() | scenario.get("bodies", {})
    server = Server(clock, shared)
    client = make_client(base_url=BASE, key=KEY, transport=httpx.MockTransport(server.handle), clock=clock)
    logs, root = _Logs(), logging.getLogger()
    level = root.level
    root.addHandler(logs)
    root.setLevel(logging.DEBUG)
    try:
        for number, step in enumerate(scenario["steps"], 1):
            where = f"{scenario['name']}, step {number} ({step['do']})"
            server.expect(step.get("exchanges", []))
            outcome, error = _do(client, clock, step)
            assert not server.problems, f"{where}: {server.problems}"
            assert not server.script, f"{where}: {len(server.script)} expected request(s) were not made"
            wanted = step.get("expect", {})
            if "raises" in wanted:
                names = {c.__name__ for c in type(error).__mro__} if error else set()
                assert wanted["raises"] in names, f"{where}: expected {wanted['raises']}, got {error!r}"
                continue
            assert error is None, f"{where}: raised {error!r}"
            if "answer" in wanted:
                _answer(where, wanted["answer"], outcome)
            if "results" in wanted:
                assert [r["result"] for r in outcome["results"]] == wanted["results"], f"{where}: {outcome}"
    finally:
        root.removeHandler(logs)
        root.setLevel(level)
    secrets = _secrets(scenario, shared)
    leaked = [m for m in logs.messages if any(s in m for s in secrets)]
    assert not leaked, f"{scenario['name']}: query or legal text was logged: {leaked}"
