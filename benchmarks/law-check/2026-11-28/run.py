"""Send each case to the live Law check checker (check_claim on /agents/mcp), one at a time, and record the verdict.
Resumable: results.jsonl. Usage: python3 run.py"""

import json
import os
import time
import urllib.error
import urllib.request

URL = "https://open.alvary.ai/agents/mcp"
UA = "OpenAlvary-citation-benchmark-bot/1.0"
FOUND = {"matches", "words_found", "words_found_with_omissions"}
cases = json.load(open("cases.json"))
done = set()
if os.path.exists("results.jsonl"):
    done = {json.loads(line)["n"] for line in open("results.jsonl")}
out = open("results.jsonl", "a")
sent = 0
for n, c in enumerate(cases):
    if n in done:
        continue
    body = {
        "jsonrpc": "2.0",
        "id": n,
        "method": "tools/call",
        "params": {
            "name": "check_claim",
            "arguments": {
                "citation": c["citation"],
                "quote": c["quote"],
                "jurisdiction": "ng",
                "reading": "Benchmark case.",
            },
        },
    }
    for attempt in range(8):
        req = urllib.request.Request(
            URL,
            data=json.dumps(body).encode(),
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        sent += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            res = {"http": e.code, "body": e.read()[:300].decode("utf-8", "replace")}
        except Exception as e:
            res = {"error": type(e).__name__}
        text = None
        try:
            text = json.loads(res["result"]["content"][0]["text"])
        except Exception:
            pass
        if text and "receipt" in text:
            break
        msg = json.dumps(res)[:300]
        if "rate_limited" in msg or res.get("http") in (429, 503):
            time.sleep(min(60, 5 * (attempt + 1)))
            continue
        break
    status = None
    quote_finding = None
    pin = None
    source = None
    err = None
    if text and "receipt" in text:
        rc = text["receipt"]
        status = rc.get("status") or text.get("status")
        quote_finding = (rc.get("findings") or {}).get("quote")
        pin = (rc.get("part") or {}).get("pinpoint")
        source = (rc.get("part") or {}).get("source_id")
    else:
        err = text or res
        err = json.dumps(err)[:400]
    rec = {
        "n": n,
        **c,
        "status": status,
        "quote_finding": quote_finding,
        "part_pinpoint": pin,
        "part_source": source,
        "error": err,
    }
    out.write(json.dumps(rec) + "\n")
    out.flush()
    print(n, c["kind"], status, quote_finding, (err or "")[:120], flush=True)
    time.sleep(1.5)
print("requests sent:", sent)
