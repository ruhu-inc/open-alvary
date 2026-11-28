"""Build citation-check benchmark cases from published Nigerian texts (read-only GETs).
Writes cases.json. Usage: python3 build.py ids.json"""

import json
import random
import re
import sys
import time
import urllib.request
from collections import Counter

BASE = "https://open.alvary.ai"
UA = "OpenAlvary-citation-benchmark-bot/1.0"
random.seed(20261128)


def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


ids = json.load(open(sys.argv[1]))
docs = []
for i in ids:
    try:
        rec = get(f"/site/v1/sources/{i}")
        txt = get(f"/site/v1/sources/{i}/text")
    except Exception as e:
        print("skip", i, e)
        continue
    time.sleep(0.3)
    b = (txt.get("canonical_text") or "").encode("utf-8")
    secs = []
    for n in txt.get("nodes") or []:
        lab = (n.get("label") or "").strip().rstrip(".")
        if n.get("kind") == "section" and re.fullmatch(r"\d+[A-Z]?", lab):
            body = b[n["start_byte"] : n["end_byte"]].decode("utf-8", "replace")
            m = re.search(r"(^|\s)" + re.escape(lab) + r"\.\s", body)
            words_from = body[m.end() :] if m else body
            secs.append((lab, body, words_from))
    if len(secs) >= 2:
        docs.append(
            {
                "id": i,
                "citation": rec.get("citation") or rec["title"],
                "type": rec.get("document_type"),
                "secs": secs,
            }
        )
print("laws with numbered sections:", len(docs), "of", len(ids))

SWAP = {
    "shall": "may",
    "may": "shall",
    "not": "also",
    "any": "every",
    "person": "citizen",
    "and": "or",
    "or": "and",
    "within": "after",
    "before": "after",
    "after": "before",
    "less": "more",
    "more": "less",
    "Minister": "Governor",
    "Federal": "State",
    "State": "Federal",
    "court": "tribunal",
    "Court": "Tribunal",
    "written": "verbal",
}


def pick_quote(body):
    words = body.split()
    if len(words) < 14:
        return None
    for _ in range(20):
        k = random.randint(10, 18)
        s = random.randint(0, max(0, len(words) - k))
        q = " ".join(words[s : s + k])
        q = q.strip(" ,;:")
        if 60 <= len(q) <= 200 and re.search(r"[a-z]{4}", q):
            return q
    return None


def alter(q, body):
    ws = q.split(" ")
    idx = [j for j, w in enumerate(ws) if re.sub(r"\W", "", w) in SWAP]
    random.shuffle(idx)
    for j in idx:
        core = re.sub(r"\W", "", ws[j])
        new = ws[:j] + [ws[j].replace(core, SWAP[core])] + ws[j + 1 :]
        a = " ".join(new)
        if norm(a) not in norm(body):
            return a, f"{core} -> {SWAP[core]}"
    # fallback: drop a word of 4+ letters
    idx = [j for j, w in enumerate(ws) if len(re.sub(r"\W", "", w)) >= 4 and 0 < j < len(ws) - 1]
    random.shuffle(idx)
    for j in idx:
        a = " ".join(ws[:j] + ws[j + 1 :])
        if norm(a) not in norm(body):
            return a, f"dropped '{ws[j]}'"
    return None, None


cases = []
for d in docs:
    secs = d["secs"]
    chosen = random.sample(secs, min(3, len(secs)))
    for lab, body, wf in chosen:
        q = pick_quote(wf)
        if not q:
            continue
        cite = f"{d['citation']}, s. {lab}"
        cases.append({"kind": "exact", "law": d["id"], "citation": cite, "quote": q, "expect": "found"})
        a, how = alter(q, body)
        if a:
            cases.append(
                {
                    "kind": "altered",
                    "law": d["id"],
                    "citation": cite,
                    "quote": a,
                    "change": how,
                    "expect": "not_found",
                }
            )
        others = [(l2, b2) for l2, b2, _ in secs if l2 != lab and norm(q) not in norm(b2)]
        if others:
            l2, _ = random.choice(others)
            cases.append(
                {
                    "kind": "wrong_section",
                    "law": d["id"],
                    "citation": f"{d['citation']}, s. {l2}",
                    "quote": q,
                    "true_section": lab,
                    "expect": "not_found",
                }
            )

FAKE = [
    "Lagos State Street Trading Law 2031",
    "Federal Tenancy (Protection) Act 2029",
    "Nigeria Market Levies Act 2030",
    "Abuja Shop Rent Control Law 2028",
    "Small Traders Licensing Act 2032",
    "Nigerian Domestic Workers Act 2030",
    "Federal Rent Tribunal Act 2029",
    "Lagos State Okada Levy Law 2031",
    "Nigeria Consumer Refunds Act 2030",
    "FCT Market Associations Law 2029",
]
real = [c for c in cases if c["kind"] == "exact"]
for t in FAKE * 2:
    q = random.choice(real)["quote"]
    cases.append(
        {
            "kind": "law_not_held",
            "citation": f"{t}, s. {random.randint(2, 40)}",
            "quote": q,
            "expect": "not_found",
        }
    )
json.dump(cases, open("cases.json", "w"), indent=1)

print(Counter(c["kind"] for c in cases))
