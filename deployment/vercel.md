# Manual Vercel deployment — metadata pilot

This target serves the React build from Vercel's CDN and the existing FastAPI routes
under `/api`. Configuration is at the repository root in `vercel.json`; the Python
entrypoint is `api.vercel:app`. Do not select `web` as the project root: that would
omit the API. No Docker, PostgreSQL, OpenAI key or DATABASE_URL is needed for this target.

## Manual upload only

GitHub stores the source code but is not connected to Vercel. Git-triggered deployments
are disabled in vercel.json. Do not import or connect the repository in the dashboard.
The local checkout is linked to `ijidai-lassas-projects/open-alvary`; `.vercel/` stays
ignored because project/account settings are machine-local.

From the repository root, upload a preview manually:

```sh
vercel deploy --scope ijidai-lassas-projects --target preview
```

On another computer, run `vercel link` and choose the existing `open-alvary` project
in that team first. If prompted to connect GitHub, decline. The initial deployment
of a newly created project may target production; use the existing project for previews.

The root vercel.json sets Framework **FastAPI** and Build Command
`python -m scripts.build_vercel`. Keep the root directory at `./`, not `web`.
Leave Output Directory and Install Command at framework defaults. No source data or
release artifacts are copied into the public static directory.

Before production, record the owner's metadata licence approval in DATA-LICENSE.md,
verify the public repository, and set these Vercel production environment variables:

| Name | Value |
|---|---|
| PUBLIC_REPOSITORY_URL | https://github.com/ruhu-inc/open-alvary |
| PUBLIC_MAINTAINER | Owner-confirmed maintainer name |
| PUBLIC_CONTACT_EMAIL | Owner-confirmed monitored public address |
| METADATA_LICENCE | CC-BY-4.0, only after approval is recorded |

Upload the approved production revision manually:

```sh
vercel deploy --prod --scope ijidai-lassas-projects
```

Production builds reject missing identity or unapproved metadata terms. Verify the
`.vercel.app` address before adding a custom domain. In Settings → Domains add the
selected hostname and apply exactly the DNS records Vercel provides.

Run `python -m scripts.smoke_public https://YOUR_HOST` against the public production
address. Verify search, source rights, downloads, About/contact and Privacy in the
browser. Keep previews protected; use authenticated Vercel tools to test them.

## Operational limits

Each function instance creates a tiny in-memory database from the committed metadata
snapshot. It has no public write API and retains no database changes. The entrypoint
rejects any source versions or legal-text structures, even if marked cleared. Use the
persistent database deployment for a later text corpus or live review operations.

Catalogue and rights updates require a new reviewed commit and redeployment. Old
Vercel deployment URLs can retain old snapshots: keep previews protected, and remove
access to superseded deployments when withdrawing metadata. A rollback must not restore
withdrawn records. These snapshots do not provide centralized, immediate rights updates
across deployment versions. Release downloads still run through integrity/rights checks
against their deployment's catalogue; no releases are copied into the static CDN directory.

Repository history is the source backup for this immutable pilot. Preserve it, record
release commits, configure an external health check on `/api/healthz`, and verify Vercel
logging/retention and traffic/spend controls. Docker nginx rate limits and log rotation
are not present in this deployment. No claim of live Vercel verification is made until
an actual deployment has passed the smoke checks.

Reference: https://vercel.com/docs/frameworks/backend/fastapi
