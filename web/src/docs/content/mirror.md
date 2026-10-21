---
title: Download and mirror the data
description: Download a release, check each file's SHA-256, and keep your copy current with the change feed.
---

To hold your own copy of the catalogue, download a **release**, check its files, then keep it current with the [change feed](/docs/changes). This is quicker than paging through every record, and the copy stays consistent.

## Releases and the manifest

A release is a set of files for one jurisdiction, made on one day: its ID is that date, such as `2026-10-05`, with `.2` and so on for a later release the same day. Each comes in a `mode`:

| `mode` | Holds |
|---|---|
| `metadata` | The catalogue records, their rights records and coverage. |
| `corpus` | The same, with the cleared texts and their structure when the release holds any. |

[List data releases](/docs/api/list-releases) gives each release still available, with its `files` and `download_base`. Without a key, the [releases page](/releases) on the website lists the same catalogue releases.

{{example:list_releases}}

Each release's `manifest.json` lists the same files. Every file has its `sha256`, and each `.jsonl` file the number of `records` it holds, one JSON object per line.

## Where files download from

Append `/` and a file name to `download_base`. The address it is relative to depends on where it points:

| `download_base` starts with | Download from | Key |
|---|---|---|
| `/downloads/` | `https://open.alvary.ai`, such as `https://open.alvary.ai/downloads/2026-10-05/ng/metadata/sources.jsonl` | None. Send no key here: a request with an `Authorization` header is refused with [`credential_not_accepted`](/docs/errors#credential_not_accepted). |
| `/api/v1/bulk/` | `https://api.open.alvary.ai` | Your key, in the `Authorization` header. |

Catalogue releases download from `/downloads/` with no account. Releases with full text download only from [`/api/v1/bulk`](/docs/api/get-bulk-file), for organisations whose access includes bulk releases, for the countries of their plan. Bulk access is granted by agreement, not by a listed plan: write to [hello@ruhu.ai](mailto:hello@ruhu.ai). Bulk downloads count towards your organisation's [data a day](/docs/plans#how-limits-are-counted).

## Download and check the files

Check every file's SHA-256 against the manifest before you use it:

```python
import hashlib
import os
import pathlib

import requests

API = "https://api.open.alvary.ai/api/v1"
KEY = {"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"}

releases = requests.get(f"{API}/releases", headers=KEY, timeout=30).json()
release = next(r for r in releases if r["jurisdiction"] == "ng" and r["mode"] == "metadata")
base = release["download_base"]
bulk = base.startswith("/api/v1/bulk/")
host = "https://api.open.alvary.ai" if bulk else "https://open.alvary.ai"
folder = pathlib.Path(release["release_id"], release["jurisdiction"], release["mode"])
folder.mkdir(parents=True, exist_ok=True)
for name, details in release["files"].items():
    response = requests.get(f"{host}{base}/{name}", headers=KEY if bulk else {}, timeout=60)
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != details["sha256"]:
        raise SystemExit(f"{name} doesn't match its checksum: download it again")
    (folder / name).write_bytes(response.content)
```

```javascript
// node mirror.mjs (Node.js 18 or later)
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";

const KEY = { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` };
const releases = await (await fetch("https://api.open.alvary.ai/api/v1/releases", { headers: KEY })).json();
const release = releases.find((r) => r.jurisdiction === "ng" && r.mode === "metadata");
const bulk = release.download_base.startsWith("/api/v1/bulk/");
const host = bulk ? "https://api.open.alvary.ai" : "https://open.alvary.ai";
const folder = `${release.release_id}/${release.jurisdiction}/${release.mode}`;
mkdirSync(folder, { recursive: true });
for (const [name, details] of Object.entries(release.files)) {
  const response = await fetch(`${host}${release.download_base}/${name}`, { headers: bulk ? KEY : {} });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  const data = Buffer.from(await response.arrayBuffer());
  if (createHash("sha256").update(data).digest("hex") !== details.sha256) {
    throw new Error(`${name} doesn't match its checksum: download it again`);
  }
  writeFileSync(`${folder}/${name}`, data);
}
```

Each file is checked against the current catalogue just before it is sent. A release with a record that has since changed or stopped being public is no longer served: its files answer `410` [`withdrawn`](/docs/errors#withdrawn). Download a newer release.

## Catch up with the change feed

A release is a copy as of its day. To keep yours current:

1. **Note where the feed is** before you download: `https://open.alvary.ai/api/changes?start=now` answers a `resume_token` at the latest change.
2. **Download and check** the release, as above.
3. **Apply every change since the token**, then keep the new token, as [Follow changes](/docs/changes#keep-a-copy-current) shows. Do this as often as you like, for example every hour, and at least daily if you serve texts to others.

Changes made between the release's day and your token aren't in what you read from the token. To close that gap, replay the feed from the earliest change it keeps (a request without `resume_token`, `cursor` or `start`; [change coverage](/docs/api/get-change-coverage) gives `earliest_replayable_at`) and apply what was committed on or after the release's day. Applying a change twice does no harm when you store changes by (`feed_epoch`, `event_id`, `event_revision`).

## Apply withdrawals and corrections

Apply these promptly, so your copy never serves what Open Alvary no longer does:

| Change | What to do |
|---|---|
| `withdrawal`, `scope_disclosure` `named` | Stop serving the text in `version` now. A corrected text, if there is one, arrives as a new `text_version_published`. |
| `withdrawal`, `scope_disclosure` `revalidate_releases` | A record stopped being public, and which one is not said. Check each record you hold with [Get a record](/docs/api/get-source); stop serving one that answers `404`. |
| `metadata_corrected` | Update the fields in `changed_fields`. |
| `text_version_published` | Get the text and check its SHA-256 equals `content_hash` (see [When a text is published or withdrawn](/docs/changes#when-a-text-is-published-or-withdrawn)). |
| `legal_effect_recorded` | Update the record's history and status. |
| A correction of a change (`change_reason` `corrected`) | Keep the highest `event_revision` of each change. |
| `409` `feed_reset` or `410` `replay_window_expired` | Download a current release, take a new `start=now` token, and continue from it. |

Every kind of change and its fields is in [Follow changes](/docs/changes#what-a-change-says).
