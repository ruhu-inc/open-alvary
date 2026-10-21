# Open Alvary on Azure

The whole Azure environment as code, kept with its Google Cloud mirror (`deployment/gcp-terraform/`)
so that Open Alvary can run on whichever cloud has credit (D33). The Azure credit runs to mid-2028;
Google Cloud's ends on 18 December 2026. Nothing here is applied without the owner's go-ahead, step by
step.

**Its role now (owner, 2026-10-19; MAC-12a).** There is no production environment until Open Alvary
accepts paying users: staging serves the public. Google Cloud staging goes first and takes over from
today's production. Azure staging is built as a standby, with the Vespa machine and the GPU off
(`engine_enabled = false`, `inference_enabled = false`; about $166 a month, by the table below).
On 1 December 2026 the owner decides:
- new Google Cloud credit means Open Alvary stays there;
- otherwise Azure staging takes the data and DNS before 18 December. Both flags go on first, and
  the domains in `staging.tfvars` become `open.alvary.ai` and `api.open.alvary.ai`.

The phases below were written for a move from today's production. They hold for a move from Google
Cloud staging too: its database, buckets and secrets are where `data.sh` and `secrets.sh` read them,
unless its Terraform moved them (check `GCP_PROJECT` and the instance names then).

## What runs where

Each environment has a resource group of its own in the subscription `ruhu-sub`, named as Ruhu's
other projects are (`open-alvary-<staging|prod>-sc-…`). It shares nothing with any other product.

| Part | On Google Cloud | On Azure |
|---|---|---|
| Public service: website, docs, REST v1, MCP and the console | Cloud Run `open-alvary-public` | Container App `open-alvary-<env>-sc-public` |
| Its jobs (migrations, worker, daily at 03:15 UTC) | Cloud Run jobs + Scheduler | Container Apps jobs (`jobs.tf`); the worker and daily job have no schedule until `domain_live` |
| Retrieval job, every minute: section index, engine sync, embeddings | (not deployed) | Container Apps job `…-retrieval` (`python -m retrieval.worker run`) |
| Search engine | (not deployed) | Vespa on one VM, private address (`engine.tf`) |
| Embedder and reranker (D29: Qwen3-Embedding-0.6B, bge-reranker-v2-m3) | (not deployed) | One T4 GPU VM, text-embeddings-inference (`inference.tf`) |
| Database | Cloud SQL PostgreSQL 16 | PostgreSQL Flexible Server 16, VNet only |
| Edge: TLS, routing, per-address throttle (D23) | Load balancer + Cloud Armor | Front Door Standard + WAF (`frontdoor.tf`) |
| Workbench | Cloud Run behind IAP, own Cloud SQL | Container App behind Entra ID sign-in, own server (`workbench.tf`) |
| Corpus bucket (staged originals, readings) | `gs://open-alvary-corpus` | Storage account `openalvary<env>sccorp…` |
| Write-once archive (CPR-05) | Locked-retention bucket | Storage container with a time-based immutability policy |
| Audit seals' copy outside the database (MAC-13.7), 800 days | Log bucket `audit-seals` and its sink | Container `audit-seals` in the archive account, immutable; the daily job writes each seal there (`storage.tf`) |
| Ingestion jobs | Cloud Run jobs | Container Apps jobs; reading stays on Google Document AI (`ingestion.tf`) |
| Secrets | Secret Manager | Key Vault; values copied, never regenerated |
| Images | Cloud Build + Artifact Registry | `az acr build` from `git archive` + Container Registry |
| Logs and alerts | Cloud Logging and Monitoring | Log Analytics, Application Insights, an availability test, a failed-job alert, the budget (`monitoring.tf`) |

Unchanged: Auth0 (sign-in), Resend (email), DNS (records change at switchover), GitHub.

## Decisions taken, and defaults

- **Resource groups.** Open Alvary gets its own resource groups in `ruhu-sub` (the owner,
  2026-10-19), with Ruhu's naming. Terraform state is in Ruhu's state account
  (`ruhu-tfstate-sc-rg`), container `open-alvary`.
- **Reading stays on Google Document AI.** The readings, the routing calibration (CPR-01) and the
  native route's thresholds were all measured on its output. The jobs reach it by workload identity
  federation (secret `google-wif-config`), so no Google key file exists. Azure Document Intelligence
  is a later, measured experiment on the same real Gazette pages.
- **Secrets are copied, never regenerated.** API keys are hashed with the key pepper, and feed tokens
  and webhook secrets are sealed with the feed and webhook keys. New values would break every key,
  token and webhook in use.
- **Vespa on one node to start** (the owner's choice). It is rebuilt from PostgreSQL by the retrieval
  job, so the machine can be replaced at any time.
- **Front Door Standard, not Premium.** Rate limiting works on Standard; managed rule sets need
  Premium (about $330 a month) and stay a hardening step. The service refuses requests that lack this
  profile's ID (`ALVARY_FRONT_DOOR_ID`, 403 `wrong_address`), so its own address is no way around the
  edge.
- **No request logs at the edge.** Front Door's access and WAF logs record each request's full
  address, query string included, and queries show what people are researching (RSV-01; the privacy
  notice promises the API and MCP keep no request logs). Azure collects a resource's logs only
  through a diagnostic setting, and Front Door has none. No setting may send its access or WAF logs
  (or a category group, which includes them) anywhere, unless a later one first excludes or redacts
  query strings; health probe logs and metrics carry no query. Container Apps sends Log Analytics only
  what the service writes, never a query or a text, and the service sends Application Insights
  nothing. `tests/test_request_logging.py` fails on a Terraform setting that would send Front Door's
  request logs; phase 3 checks that none was added outside Terraform. On Google Cloud the same
  entries are excluded before they are stored (`google_logging_project_exclusion.api_request_urls`).
- **The workbench has its own database server**, apart from the public service's, as on Google
  Cloud.
- **No vector profile is active** until a generation passes its gates (RSV-12): `vector_profile`
  stays empty and the service runs on the lexical arms with the reranker.

## Found on 2026-10-19 (read-only checks)

- **`ruhu-sub` is a Sponsorship subscription** with no spending limit. Azure Cost Management does
  not report on it, so the budget resource is off (`budget_enabled`). Usage and the credit left are
  in Microsoft's sponsorship portal. With no spending limit, use past the credit is billed: watch the
  portal.
- **GPU quota is 0** for the NCASv3_T4 family in Sweden Central: request it before phase 1.
- **Azure Document Intelligence is already a reader.** `open-alvary-docintel-sc` (S0, in
  `open-alvary-ingestion-sc-rg`) is the second independent check beside Document AI and MinerU
  (deployment/runbook.md). Its agreement with Document AI is measured on every reconciled Act, so
  making it the primary reading later is a measured step.

## What is live on Google Cloud (listed 2026-10-19, read-only)

| Project | Running | Data |
|---|---|---|
| `open-alvary-prod` | `open-alvary-public` (website, docs, REST, MCP, console: activation B is on); jobs `migrate`, `worker`, `daily`, `access-role`; Scheduler every 5 minutes and 03:15; load balancer `35.244.181.134` | Cloud SQL `open-alvary-db` (Postgres 16, 10 GB disk) |
| `open-alvary-ingestion` | `review-workbench`; jobs `scan-reader`, `corpus-preparer`, `workbench-admin`, `workbench-enrol` | Cloud SQL `review-db` (Postgres 16); buckets `open-alvary-corpus` (18.4 GB), `open-alvary-ingestion-review-collection` (111 MB) |
| `open-alvary-staging` | nothing (no APIs enabled) | |

- **No archive bucket exists**, so nothing on Google Cloud is locked. Azure's archive starts empty
  and is filled by ingestion.
- **DNS.** `open.alvary.ai` and `api.open.alvary.ai` point at the load balancer. The domain is
  registered and its DNS hosted at Squarespace Domains (Google Domains' former name servers), apart
  from any Google Cloud project, so shutting Google Cloud down does not touch DNS. Switchover is a
  record change there.
- **Secret Manager** holds every secret `secrets.sh` copies: the Auth0 deletion pair goes in
  `optional_secrets`; `database-*` and `review-database-url` are made anew for the new servers.
- **The workbench's first collection** (the review-collection bucket, mounted read-only on Cloud
  Run) goes to `az://<corpus account>/workbench/collections/ng/` and is mirrored into the
  workbench's cache at start (`ALVARY_REVIEW_STAGING`); Rwanda's stays at `collections/rw/` in the
  corpus copy.

## Known issue: dumps and row-level security (found 2026-10-20)

`data.sh dump` and `restore.py dump` cannot copy today's public database as written. Its private
tables admit only the guard role (`USING current_user = '<prefix>_guard'`, with `FORCE ROW LEVEL
SECURITY`). Neither Cloud SQL nor Flexible Server gives customers a user that bypasses row-level
security, so `pg_dump` stops at the first of those tables. Google Cloud to Google Cloud now moves by
backup instead (`deployment/gcp-terraform/data.sh backup`). A move to Azure needs a way first, for
example dumping each schema as the role its policies admit. Settle it before the 1 December decision.

## Still to settle with the owner

- **Alvary's spend.** The credit is shared: about $4.7K a month to mid-2028. Open Alvary's estimate
  is below. Check both products' usage in the sponsorship portal.
- **The penetration test.** It moves to the Azure environment, after switchover.

## Cost per month (Sweden Central list prices, checked 2026-10-19 in Azure's retail price list; 730 hours)

| Part | Phase | Staging | Production |
|---|---|---|---|
| Vespa VM D4s v5 ($0.204/h), 128 GB data disk (P10), OS disk (P4) | 1 | ~$176 | ~$176 |
| PostgreSQL Flexible B2s ($0.0398/h) and 32 GB storage | 1 | ~$33 | ~$33 (+ geo backups) |
| Front Door Standard base fee, WAF policy, little traffic | 1 | ~$35 | ~$40 |
| NAT gateway and its address (the VMs' outbound) | 1 | ~$37 | ~$37 |
| Registry Basic, Key Vault, storage (18 GB corpus), logs, DNS zone | 1 | ~$12 | ~$20 |
| Container Apps: one warm replica (1 vCPU, 2 GiB), jobs every minute | 3 | ~$30 | ~$45 |
| Workbench: PostgreSQL B1ms ($0.0199/h) and storage; app scales to zero | 4 | ~$19 | ~$19 |
| GPU VM NC4as T4 v3 ($0.558/h) and 128 GB OS disk | when quota | ~$429 | ~$429 |
| **Standby** (Vespa and the GPU off: foundation without the VM, apps, workbench) | | **~$166** | — |
| **Total without the GPU** | | **~$342** | **~$370** |
| **Total with the GPU** | | **~$771** | **~$799** |

- Container Apps environments on the consumption plan have no fixed fee that the price list shows
  (its $0.13/h meters are for dedicated plans, private endpoints and maintenance windows). The
  monthly free grant (180,000 vCPU-seconds, 360,000 GiB-seconds) is shared with the subscription's
  other apps.
- Parking staging between rehearsals: deallocating both VMs leaves only their disks (~$27, plus ~$22
  for the GPU's), and PostgreSQL can be stopped for up to 7 days at a time. Front Door's base fee and
  the NAT gateway keep running.
- Serverless T4 GPUs on Container Apps cost more kept warm: about $1.64/h with their fixed 8 vCPU and
  56 GiB, against the VM's $0.56.

## Before the first apply (owner)

1. **GPU quota.** The NCASv3_T4 family in Sweden Central: 8 vCPUs, one machine per environment
   (requested by support ticket, 2026-10-19). Until it is granted, apply with
   `inference_enabled = false`: no GPU machine, and the service runs on the lexical arms without a
   reranker, saying so in every pack.
2. **Settings.** `cp env/staging.tfvars.example env/staging.tfvars` and fill it in. Do the same for
   `backend.hcl` from `backend.hcl.example`. `operator_ip_ranges` must hold the address you apply
   from (`curl -s https://ifconfig.me`): the vault and storage refuse every other one, Terraform's
   own requests included. Set `domain_live = true` only once DNS points at Front Door (phase 6).
   Until then the worker and daily job are manual jobs that never start on their own (Google Cloud
   pauses their schedules instead), so an environment being readied beside the live one sends no
   digests or webhooks and purges nothing.
3. **Pins to check.** `tei_image` must be a text-embeddings-inference release for Turing GPUs that
   lists Qwen3-Embedding among its supported models; `vespa_image` is the release the tests ran on.

## Phases

Each phase ends with a check, and nothing serves the public until phase 6.

1. **Foundation.**
   - `cd bootstrap && terraform init && terraform apply`;
   - then, in `env/`: `terraform init -backend-config=../backend.hcl -backend-config="key=staging.tfstate"`
     and `terraform apply -var-file=staging.tfvars` with `public_image` empty.
   - This creates the network, vault, registry, logs, budget, database, storage, Front Door, Vespa
     and the GPU machine.
   - Check: the retrieval VMs answer from inside the network. Run `az vm run-command` on the engine
     to `curl` 8080, and on the GPU machine to `curl` 8080 and 8081.
2. **Secrets.** `./secrets.sh plan staging`, then `./secrets.sh copy staging`. Values go from Secret
   Manager to Key Vault through a pipe, never printed or written to disk.
3. **Public service.**
   - `./release.sh staging public` builds the image from the commit and runs the migrations job;
   - then set `public_image` and apply.
   - Check: the Front Door endpoint serves `/healthz` and the website, and the full test and smoke
     suites pass against it.
   - Check: the service sees each visitor's own address (MAC-12: second from the right in
     `X-Forwarded-For`, which holds when Front Door appends the client's address and the ingress
     appends Front Door's). Compare what the service records with `X-Azure-ClientIP` before the
     website's throttle and admission depend on it.
   - Check: a request to the Container App's own address is refused (403 `wrong_address`).
   - Check: Front Door has no diagnostic setting, so no request log is kept.
     `az monitor diagnostic-settings list --resource "$(az afd profile show -g open-alvary-<env>-sc-rg
     --profile-name open-alvary-<env>-sc-fd --query id -o tsv)"` lists none.
   - Check, once `accounts_enabled` is on: a failed job run emails the operator. Start the worker
     once with its command replaced by one that fails (`az containerapp job start` with `--image`
     set to `public_image`, `--container-name worker` and `--command false`); the email should
     come within 15 minutes. If none does, find the run's rows in `ContainerAppSystemLogs_CL` and
     correct the reasons or job column in `monitoring.tf`'s `job_failed` query.
4. **Workbench.** The storage adapter (`az://`) and Entra ID sign-in. Set `workbench_image`,
   `workbench_domain` and `workbench_entra_client_id`, then apply. Check: an operator signs in and
   reviews a page.
5. **Data** (`./data.sh plan <env>` lists it). Rehearse on staging, then repeat for production.
   - Build the restore and ingestion images (`./release.sh <env> restore`, `./release.sh <env>
     ingestion`), set `restore_image` and `ingestion_image`, and apply.
   - Databases:
     - `cloud-sql-proxy <instance> --port 55432` (55433 for the review database);
     - `./data.sh dump <env> public`, then `./data.sh restore <env> public`;
     - the same with `review`;
     - then `./release.sh <env> public` runs the migrations against the restored schema.
     The restore recreates the source's roles (only the access role logs in), loads the dump
     without owners, and fails on any table whose rows differ from the source's.
   - Buckets: `./data.sh buckets <env>` copies both inside Azure; a second run copies only what is
     new. `./data.sh verify <env>` checks every object's MD5. The copy job reads Google Cloud
     Storage through workload identity federation (`google-wif-config`, below).
   - Check: the restore and verify pass, and the benchmarks give the same metrics through the new
     service.
6. **Switchover.**
   - Add the validation TXT records `terraform output dns_records` prints;
   - then move the CNAMEs of `open.alvary.ai` and `api.open.alvary.ai` to the Front Door endpoint.
   - In the same change, set `domain_live = true` and apply. The availability test and its alert
     start, and the worker and daily job are made again with their schedules (a job's trigger cannot
     change in place).
   - Pause the worker's and daily job's schedules on Google Cloud: `domain_live = false` in its
     Terraform, or for today's `open-alvary-prod` the `gcloud scheduler jobs pause` commands in
     `deployment/gcp-terraform/README.md` (phase 6). Only one environment sends at a time.
   - Check, after the first daily run with accounts on: `audit-seals` holds a `<day>.json` for every
     seal in `accounts.audit_seal`, and the run's `audit seals` line counts what it copied.
   - Keep Google Cloud read-only as a fallback until 18 December, then shut it down. Moving the
     CNAMEs back, with `domain_live = false` here and Google Cloud's schedules resumed, is the
     rollback.

## Google from Azure (owner, once)

The ingestion and copy jobs reach Google Cloud (Document AI, the buckets) as the ingestion job's
managed identity, by workload identity federation: Google's token service exchanges its Entra
token for a short-lived Google one, so no Google key exists anywhere. After phase 1:

1. **An Entra application for the audience**, with no secret and no permissions:
   `az ad app create --display-name open-alvary-google-federation --identifier-uris api://open-alvary-google`.
2. **In `open-alvary-ingestion`**, a pool and a provider that admit only the ingestion identity.
   `terraform output ingestion_identity` gives the principal ID and the tenant ID.
   ```
   gcloud iam workload-identity-pools create azure --location=global --display-name="Azure (Open Alvary)"
   gcloud iam workload-identity-pools providers create-oidc azure --location=global \
     --workload-identity-pool=azure --issuer-uri="https://sts.windows.net/<tenant ID>/" \
     --allowed-audiences="api://open-alvary-google" --attribute-mapping="google.subject=assertion.sub" \
     --attribute-condition="assertion.sub == '<principal ID>'"
   ```
3. **Let that principal act as the jobs' existing service account**, which already holds Document
   AI and the corpus bucket:
   ```
   gcloud iam service-accounts add-iam-policy-binding <jobs' service account> \
     --role=roles/iam.workloadIdentityUser \
     --member="principal://iam.googleapis.com/projects/<project number>/locations/global/workloadIdentityPools/azure/subject/<principal ID>"
   ```
4. **The credential configuration.** It names a command, not a key:
   ```
   gcloud iam workload-identity-pools create-cred-config \
     projects/<project number>/locations/global/workloadIdentityPools/azure/providers/azure \
     --service-account=<jobs' service account> --executable-command="python -m ingestion.google_federation token" \
     --executable-timeout-millis=30000 --output-file=google-wif-config.json
   az keyvault secret set --vault-name open-alvary-<stg|prod>-sc-kv --name google-wif-config --file google-wif-config.json
   ```

## Locking the archive (owner, irreversible)

After phase 5's archive copy checks out, lock the policy: set `locked = true` in `storage.tf`'s
`azurerm_storage_container_immutability_policy.archive` and apply. Once locked, the retention period
can only be extended, and no one, including the owner, can delete an archived original before it
ends. Originals with personal data go to the corpus account instead (CPR-05: lawful erasure stays
possible).

The audit seals' container is locked the same way, once the first seals are in it:
`azurerm_storage_container_immutability_policy.audit_seals`, 800 days, as Google Cloud's
`audit-seals` log bucket. The first daily run on Azure, after switchover, also writes every seal the
restored database holds, so the copy covers the chain from its first day.

## Files

- `bootstrap/`: the state container.
- `env/`: the environment.
  - `main.tf`, `network.tf`, `monitoring.tf`, `keyvault.tf`, `registry.tf`, `identity.tf`,
    `postgres.tf`, `storage.tf`;
  - `engine.tf` and `inference.tf` with their cloud-init templates;
  - `container_apps.tf`, `jobs.tf`, `frontdoor.tf`, `workbench.tf`, `ingestion.tf`;
  - `variables.tf`, `outputs.tf`, and `staging.tfvars.example` and `prod.tfvars.example`.
- `secrets.sh`: copies secrets from Secret Manager to Key Vault.
- `release.sh`: builds an image from the commit, runs the migrations, and gives the image to apply.
- `data.sh`: the database dump and restore and the bucket copies, with their checks;
  `restore.py` and `restore.Dockerfile`: the restore job; `operations/transfer.py`: the copy.
