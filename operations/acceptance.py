"""The Alvary integration's done-when checks (open-alvary-integration-spec.md, OA-S1–OA-A1), run
against a live REST v1 with a key, or any client answering the same routes.

  OPEN_ALVARY_KEY=oa_live_… python -m operations.acceptance --base https://api.open.alvary.ai/api/v1 \
      --citation "NDPA s. 23(1)(a)" [--golden golden.json]

Each check reads only public routes and prints PASS, FAIL or SKIP with what it saw; the exit status is 1
when any check fails. Nothing is written. A golden file is a list of {"query", "expect"}: the search
passes when one of the top three hits has a pinpoint in "expect" (or "document" for the text's own
opening words), within --seconds.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

# The NDPA's golden queries (OA-S1): a statute by its short title, a section by number, a concept.
NDPA = [
    {"query": "Nigeria Data Protection Act", "expect": ["document", "s. 66"]},
    {"query": "Nigeria Data Protection Act, 2023, s. 25", "expect": ["s. 25"]},
    {"query": "lawful basis processing consent", "expect": ["s. 25"]},
]


@dataclass
class Report:
    results: list = field(default_factory=list)

    def add(self, item: str, outcome: str, detail: str) -> None:
        self.results.append((item, outcome, detail))
        print(f"{outcome:4}  {item:34} {detail}")

    @property
    def failed(self) -> bool:
        return any(outcome == "FAIL" for _, outcome, _ in self.results)


def run(client, citation: str, golden: list[dict], seconds: float = 1.0) -> Report:
    """Every check, in order; ``client`` answers get(path, params=, headers=) as httpx does."""
    report = Report()

    def get(path, **params):
        started = time.perf_counter()
        response = client.get(path, params=params or None)
        return response, time.perf_counter() - started

    # OA-S2: a pinpoint resolves to one part, whose span is the text's exact bytes.
    answer, _ = get(f"/citations/{quote(citation, safe='')}")
    provision = answer.json().get("provision") if answer.status_code == 200 else None
    part = None
    if provision and provision["status"] == "resolved":
        part = provision["candidates"][0]
        report.add("OA-S2 provision lookup", "PASS", f"{citation} → {part['citation']} ({part['locator']})")
    else:
        status = provision["status"] if provision else answer.status_code
        report.add("OA-S2 provision lookup", "FAIL", f"{citation}: {status}, {json.dumps(provision)[:200]}")
        return report
    text, _ = get(f"/versions/{part['version_id']}/text")
    raw = text.content
    span = raw[part["start_byte"] : part["end_byte"]]
    exact = text.status_code == 200 and hashlib.sha256(span).hexdigest() == part["span_hash"]
    exact = exact and hashlib.sha256(raw).hexdigest() == part["content_hash"]
    report.add(
        "OA-S2 exact span",
        "PASS" if exact else "FAIL",
        f"bytes {part['start_byte']}–{part['end_byte']} of {len(raw):,}: {span[:70].decode(errors='replace')!r}",
    )

    # OA-T1/AL-2: the part is an anchor, and the anchor's words are the same bytes.
    anchor, _ = get(f"/anchors/{part['anchor_id']}")
    same = anchor.status_code == 200 and anchor.json()["text"].encode() == span
    report.add(
        "OA-T1 anchor", "PASS" if same else "FAIL", f"/anchors/{part['anchor_id']} → {anchor.status_code}"
    )

    # OA-T2: the text answers 304 to its ETag, after the same check that it is still public.
    tag = text.headers.get("etag")
    again = client.get(f"/versions/{part['version_id']}/text", headers={"If-None-Match": tag or ""})
    report.add(
        "OA-T2 conditional request",
        "PASS" if tag and again.status_code == 304 else "FAIL",
        f"ETag {tag} → {again.status_code}",
    )

    # OA-T3: a date and a language select one text or say why not; the edition is listed.
    source = part["source_id"]
    chosen, _ = get(f"/sources/{source}/versions", language=part["language"])
    selection = chosen.json().get("selection") if chosen.status_code == 200 else None
    editions, _ = get(f"/sources/{source}/expressions")
    listed = editions.status_code == 200 and any(
        e["expression_id"] == f"{source}:{part['language']}" for e in editions.json()["items"]
    )
    report.add(
        "OA-T3 version selection",
        "PASS" if selection and selection["status"] in ("selected", "ambiguous") and listed else "FAIL",
        f"language={part['language']}: {selection and selection['status']}"
        f" {selection and (selection.get('reason') or selection.get('version_id'))}; edition listed: {listed}",
    )

    # OA-R1: the rights record has a history.
    history, _ = get(f"/rights/{source}/history")
    revisions = history.json().get("revisions", []) if history.status_code == 200 else []
    report.add("OA-R1 rights history", "PASS" if revisions else "FAIL", f"{len(revisions)} revision(s)")

    # OA-M1: the change feed announced the text that is served.
    changes, _ = get("/changes", work=source, type="text_version_published", limit=100)
    events = changes.json().get("events", []) if changes.status_code == 200 else []
    named = [e for e in events if (e.get("version") or {}).get("id") == part["version_id"]]
    report.add(
        "OA-M1 change feed",
        "PASS" if named and named[0]["version"].get("content_hash") == part["content_hash"] else "FAIL",
        f"{len(named)} text_version_published event(s) for the version",
    )

    # OA-S1: golden queries find the expected part in the top three, within the time allowed.
    for case in golden:
        page, elapsed = get("/search/text", q=case["query"])
        hits = page.json().get("items", [])[:3] if page.status_code == 200 else []
        found = [h.get("pinpoint") or ("document" if h["kind"] == "document" else h["kind"]) for h in hits]
        ok = any(f in case["expect"] for f in found) and elapsed <= seconds
        report.add(
            "OA-S1 search",
            "PASS" if ok else "FAIL",
            f"{case['query']!r} → {found} in {elapsed * 1000:.0f} ms (expected one of {case['expect']})",
        )
    if not any(case.get("language") == "fr" for case in golden):
        report.add("OA-S1 French query", "SKIP", "no French text is published in this corpus")

    # OA-A1: the allowance a mirror is sized against is published.
    policy, _ = get("/quota-policy")
    bytes_policy = policy.json().get("bytes") if policy.status_code == 200 else None
    report.add("OA-A1 data a day", "PASS" if bytes_policy else "FAIL", json.dumps(bytes_policy)[:120])
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--base", required=True, help="REST v1 root, such as https://api.open.alvary.ai/api/v1"
    )
    parser.add_argument("--citation", required=True, help='A pinpoint citation, such as "NDPA s. 23(1)(a)"')
    parser.add_argument("--golden", type=Path, help="Golden queries as JSON; the NDPA's by default")
    parser.add_argument("--seconds", type=float, default=1.0, help="The time a search may take")
    args = parser.parse_args(argv)
    key = os.environ.get("OPEN_ALVARY_KEY")
    if not key:
        print("Set OPEN_ALVARY_KEY to an API key (oa_live_…) from the console.", file=sys.stderr)
        return 2
    import httpx

    golden = json.loads(args.golden.read_text()) if args.golden else NDPA
    with httpx.Client(
        base_url=args.base.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=30
    ) as client:
        report = run(client, args.citation, golden, args.seconds)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
