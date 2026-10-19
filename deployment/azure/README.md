# Open Alvary on Azure

Open Alvary moves from Google Cloud to Azure, decided by the owner on 2026-10-19. The Azure credit
runs to mid-2028; Google Cloud's ends on 18 December 2026. This directory is the whole environment as
code. Nothing here is applied without the owner's go-ahead, step by step.

## What runs where

Each environment has a resource group of its own in the subscription `ruhu-sub`, named as Ruhu's
other projects are (`open-alvary-<staging|prod>-sc-…`). It shares nothing with any other product.

| Part | On Google Cloud | On Azure |
|---|---|---|
| Public service: website, docs, REST v1, MCP and the console | Cloud Run `open-alvary-public` | Container App `open-alvary-<env>-sc-public` |
| Its jobs (migrations, worker, daily) | Cloud Run jobs + Scheduler | Container Apps jobs (`jobs.tf`) |
| Retrieval job, every minute: section index, engine sync, embeddings | (not deployed) | Container Apps job `…-retrieval` (`python -m retrieval.worker run`) |
| Search engine | (not deployed) | Vespa on one VM, private address (`engine.tf`) |
| Embedder and reranker (D29: Qwen3-Embedding-0.6B, bge-reranker-v2-m3) | (not deployed) | One T4 GPU VM, text-embeddings-inference (`inference.tf`) |
| Database | Cloud SQL PostgreSQL 16 | PostgreSQL Flexible Server 16, VNet only |
| Edge: TLS, routing, per-address throttle (D23) | Load balancer + Cloud Armor | Front Door Standard + WAF (`frontdoor.tf`) |
| Workbench | Cloud Run behind IAP, own Cloud SQL | Container App behind Entra ID sign-in, own server (`workbench.tf`) |
| Corpus bucket (staged originals, readings) | `gs://open-alvary-corpus` | Storage account `openalvary<env>sccorp…` |
| Write-once archive (CPR-05) | Locked-retention bucket | Storage container with a time-based immutability policy |
| Ingestion jobs | Cloud Run jobs | Container Apps jobs; reading stays on Google Document AI (`ingestion.tf`) |
| Secrets | Secret Manager | Key Vault; values copied, never regenerated |
| Images | Cloud Build + Artifact Registry | `az acr build` from `git archive` + Container Registry |
| Logs and alerts | Cloud Logging and Monitoring | Log Analytics, Application Insights, an availability test, the budget |

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

## Still to settle with the owner

- **Alvary's spend.** The credit is shared: about $4.7K a month to mid-2028. Open Alvary's estimate
  is below. Check both products' usage in the sponsorship portal.
- **The penetration test.** It moves to the Azure environment, after switchover.

## Cost, estimated per month (Sweden Central, list prices, low traffic)

| Part | Staging | Production |
|---|---|---|
| GPU VM, NC4as T4 v3, always on | ~$380 | ~$380 |
| Vespa VM, D4s v5, and 128 GB disk | ~$160 | ~$160 |
| PostgreSQL B2s, and the workbench's B1ms | ~$45 | ~$45 (+ geo backups) |
| Container Apps (one warm replica, jobs) | ~$40 | ~$60 |
| Front Door Standard and WAF policy | ~$40 | ~$45 |
| NAT gateway and address | ~$35 | ~$35 |
| Storage, registry, Key Vault, logs | ~$25 | ~$40 |
| **Total** | **~$725** | **~$765** |

Staging can be stopped between rehearsals; the VMs then cost only their disks. Check these with
Azure's pricing calculator before the first apply.

## Before the first apply (owner)

1. **GPU quota.** The NCASv3_T4 family in Sweden Central: 8 vCPUs, one machine per environment
   (requested by support ticket, 2026-10-19). Until it is granted, apply with
   `inference_enabled = false`: no GPU machine, and the service runs on the lexical arms without a
   reranker, saying so in every pack.
2. **Settings.** `cp env/staging.tfvars.example env/staging.tfvars` and fill it in. Do the same for
   `backend.hcl` from `backend.hcl.example`. `operator_ip_ranges` must hold the address you apply
   from (`curl -s https://ifconfig.me`): the vault and storage refuse every other one, Terraform's
   own requests included. Set `domain_live = true` only once DNS points at Front Door.
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
   - Keep Google Cloud read-only as a fallback until 18 December, then shut it down. Moving the
     CNAMEs back is the rollback.

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
