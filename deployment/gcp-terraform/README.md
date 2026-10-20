# Open Alvary on Google Cloud, as code

Open Alvary's whole architecture, as code on Google Cloud. It is written as Terraform piece for piece
beside deployment/azure, which holds the same environment for Azure, so Open Alvary can run on
whichever cloud has credit (D33) and move between them (only one is live at a time). Nothing here
has been applied, and nothing is applied without the owner's go-ahead, step by step. Saved plans
(`*.plan`) stay local: they can hold secret values.

**Staging is the live environment until there are paying users** (the owner, 2026-10-19). It serves
`open.alvary.ai` and `api.open.alvary.ai`, uses today's buckets in place, and runs Vespa and a T4
GPU. There is no production. `prod` stays possible through `prod.tfvars`, in a fresh project, and
takes the public domains over from staging when it comes. Azure staging is built as the standby,
with its Vespa machine and GPU off (deployment/azure/README.md, MAC-12a). On 1 December 2026 the
owner decides whether Open Alvary stays here, or moves there before Google's credit ends on 18
December.

**Never point this code at `open-alvary-prod` or `open-alvary-ingestion`.** Today's live system was
built by the bash scripts in deployment/gcp. Applying here against those projects would try to
create what already exists there, or replace it. Adopting them would take an import plan, resource
by resource. `project_id` refuses both names, so removing that guard is a deliberate step. Each
environment targets a fresh project of its own. `open-alvary-staging` qualifies: it exists, is
empty, and is billed to the same billing account as today's production.

## One cloud at a time

Only one environment, in one cloud, serves the public at a time. The worker sends digests and
webhooks. The daily job seals the audit log and purges what has expired. Two of either would send
everything twice and let two databases drift apart. So:

- In this environment, the worker's and daily job's schedules stay paused until `domain_live` is
  true. Set it in the same change that points DNS here (phase 6).
- At switchover, pause the schedules in the environment being left, whether today's
  `open-alvary-prod` or Azure. Keep it read-only as the fallback.
- A rehearsal holding production's data must never send anything. Here that is guaranteed by the
  paused schedules. On Azure, set `accounts_enabled = false`.

## What runs where

| Part | Today (deployment/gcp/*.sh) | On Azure (deployment/azure) | Here (deployment/gcp-terraform) |
|---|---|---|---|
| Boundary | Projects `open-alvary-prod`, `open-alvary-ingestion` | Resource group per environment | A fresh project per environment (`project_id`) |
| Network | None of its own (the Cloud SQL connector) | VNet, delegated subnets | VPC `open-alvary-<env>-vpc`; Cloud Run by Direct VPC egress (`network.tf`) |
| Public service | Cloud Run `open-alvary-public` | Container App | Cloud Run `open-alvary-<env>-public` (`cloud_run.tf`) |
| Its jobs | Cloud Run jobs + Scheduler | Container Apps jobs | Cloud Run jobs + Cloud Scheduler (`jobs.tf`) |
| Retrieval job, every minute | (not deployed) | Container Apps job | Cloud Run job `…-retrieval` (`python -m retrieval.worker run`) |
| Search engine | (not deployed) | Vespa VM | Vespa on Compute Engine, private address, 128 GB pd-ssd (`engine.tf`) |
| Embedder and reranker (D29) | (not deployed) | T4 VM | One T4 (or L4) VM, text-embeddings-inference (`inference.tf`) |
| Database | Cloud SQL 16, public IP through the connector | Flexible Server 16, VNet only | Cloud SQL 16, private IP only (Private Service Access) (`postgres.tf`) |
| Edge (TLS, routing, D23 throttle) | Load balancer + Cloud Armor | Front Door + WAF | Global HTTPS load balancer, serverless NEG, Certificate Manager, Cloud Armor (`edge.tf`) |
| Workbench | Cloud Run behind IAP, own Cloud SQL | Container App behind Entra ID, own server | Cloud Run behind IAP, own Cloud SQL (`workbench.tf`) |
| Corpus bucket | `gs://open-alvary-corpus` | Storage account | Today's, used in place, or `open-alvary-<env>-corpus` (`storage.tf`) |
| First collection | `gs://open-alvary-ingestion-review-collection` | `az://…/workbench/collections/ng/` | Today's, used in place and mounted, or `open-alvary-<env>-review-collection` |
| Write-once archive (CPR-05) | (none yet) | Container with immutability policy | `open-alvary-<env>-archive`, retention policy, unlocked |
| Database copy | (none) | Transfer container, 14 days | `open-alvary-<env>-transfer`, 14 days (`transfer.tf`) |
| Ingestion jobs | Cloud Run jobs | Container Apps jobs, Document AI by federation | Cloud Run jobs, Document AI natively (`ingestion.tf`) |
| Secrets | Secret Manager | Key Vault, values copied | Secret Manager containers, values copied (`secrets.tf`, `secrets.sh`) |
| Images | Cloud Build + Artifact Registry | `az acr build` + Container Registry | Cloud Build from `git archive` + Artifact Registry (`registry.tf`, `release.sh`) |
| Machines' outbound | (none) | NAT gateway | Cloud NAT with a static address |
| Logs and alerts | Logging, Monitoring | Log Analytics, availability test | Uptime check, alert policies, audit-seal bucket, budget (`monitoring.tf`) |
| Identities | `public-api`, `workbench-runner`, `scan-reader` | Managed identity per workload | Service account per workload (`identity.tf`) |

Unchanged in every case: Auth0 (sign-in), Resend (email), DNS at Squarespace (records change at
switchover), GitHub.

## Where Google Cloud differs from Azure, and why

- **The edge.**
  - Cloud Run's ingress takes internal and load-balancer traffic only, and its run.app address is
    off, so Cloud Armor cannot be bypassed: `ALVARY_FRONT_DOOR_ID` is never set here (that check is
    Azure-only).
  - The certificates are Certificate Manager's, authorised by a CNAME per domain.
  - The authorisations are per project (`PER_PROJECT_RECORD`), so this project's certificates can
    be issued while today's `open-alvary-prod` still holds its own. Switchover is then an A record,
    with no window without a certificate.
- **The database.**
  - Cloud SQL has no extension allow-list to set. `pg_trgm` and `vector` are both on its list of
    supported extensions, and `open_alvary` (a member of `cloudsqlsuperuser`) creates them when the
    migrations ask.
  - The service connects over TCP to the private address with `sslmode=require` (the instance
    allows encrypted connections only). It does not use the connector's socket, which needs a
    public address.
  - The password is alphanumeric, for `scripts.check_launch`.
- **The buckets** are guarded by IAM alone, with uniform access and public access prevented.
  - Bucket IP filtering would cut off the Cloud Run jobs that have no VPC egress, so there are no
    network rules, unlike Azure's storage firewall.
  - The archive's retention policy rules out versioning (Google allows one or the other), and
    nothing in it can be replaced anyway.
  - Today's corpus and collection buckets can be used in place (`existing_corpus_bucket`,
    `existing_review_bucket`): Terraform adds this project's accounts to them, member by member,
    and never imports or changes the buckets. Their storage stays billed to `open-alvary-ingestion`,
    on the same billing account.
- **The workbench** keeps today's arrangement: IAP on Cloud Run itself, at the service's run.app
  address, and the first collection mounted read-only from its bucket.
  - The mount is used, not the `gs://` mirror (`workbench/content.py` `mirror`), because Cloud Run's
    `/tmp` is memory: a 111 MB copy at every cold start would take memory from a 2 GiB instance.
    `ALVARY_REVIEW_STAGING` can still be set to a `gs://` place in `workbench_settings`.
  - The cache is capped at 512 MB for the same reason.
  - There is no custom domain. One would need the load balancer, with IAP moved onto its backend.
- **Reading** is Document AI, called natively as the ingestion account: no key and no federation.
  Today's processors are in `open-alvary-ingestion`, so Terraform grants Document AI's user role
  there (`ALVARY_DOCAI_PROJECT`).
- **The GPU machine.**
  - It boots a Deep Learning VM image (Ubuntu 22.04, NVIDIA driver 580), in place of Azure's driver
    extension.
  - cloud-init adds Docker and NVIDIA's container toolkit only where the image lacks them. It
    refreshes packages without upgrading them: a kernel or driver upgrade under a running GPU
    breaks it until a reboot.
  - The machine stops and starts for host maintenance (GPUs cannot live-migrate).
- **SSH** to the machines is through IAP's TCP forwarding with OS Login, from IAP's range only. The
  machines have no public address, and no admin key is needed (Azure's `admin_ssh_public_key`).
- **The engine has a switch** (`engine_enabled`), like the GPU machine's.
- **The Vespa machine** is `e2-standard-4` (4 vCPU, 16 GiB, $108 a month), against Azure's D4s v5.
  E2 gets no sustained-use discount, but its list price is lower than `n2-standard-4`.
- **The budget works.** Billing budgets work on credit-funded accounts (Azure's Sponsorship offer
  has no Cost Management). This one measures cost before credits, so it shows what the credits are
  paying for.
- **Also kept from today's live system**, which Azure lacks:
  - REST v1 and MCP request logs are dropped before storage (OA-A2);
  - the audit-seal log bucket (MAC-13.7, 800 days, unlocked);
  - the failed-job alert.
- **The daily job runs at 03:15 UTC**, as today; Azure's runs at 03:00.
- **Not mirrored:** `deployment/gcp/slo.sh` (D30 objectives) and `regions.sh` (a second region).
  Both are the owner's later steps. Neither exists on Azure.

## Choices for the owner to review

- **T4 or L4** (`inference_gpu`).
  - T4: about $299 a month with the sustained-use discount, the same GPU as Azure. It is the
    default, as directed.
  - L4: about $581. TEI's Turing build (`turing-1.8`) is marked experimental upstream; its Ada
    build (`89-1.8`, for the L4) is not.
  - Both are in europe-west1-b. `open-alvary-prod` has quota for both today; `open-alvary-staging`
    is unknown.
- **IAP on Cloud Run** (as today), or IAP on the load balancer. The load balancer would allow a
  custom domain such as `review.open.alvary.ai`, at the cost of a second backend and certificate.
- **The Cloud SQL tiers.**
  - Public: `db-g1-small` (shared core, 1.7 GB, about 50 connections, no SLA), which is what
    production runs today and smaller than Azure's B2s. It is zonal. A dedicated-core tier such as
    `db-custom-1-3840` (1 vCPU, 3.75 GB) would cost about $50 a month, about $25 more (an estimate
    from the same price list). A regional (HA) instance doubles the cost.
  - Workbench: `db-f1-micro`, as today.
- **The engine machine:** `e2-standard-4`, or `n2-standard-4` for steadier CPU (about $156 a month
  at list price, about $125 with the sustained-use discount; an estimate).
- **The workbench's 2 GiB and 512 MB cache**, and the public service's 1 GiB (today's size;
  Azure's is 2 GiB).
- **The ingestion account's write on today's corpus bucket.** It sits beside today's live ingestion
  account until one of them is retired.
- **The budgets** (1,200 for staging, in the billing account's currency) and the archive's 10-year
  retention. Locking that retention is irreversible.

## Cost per month (estimate)

These are europe-west1 list prices, read from Google's Cloud Billing Catalog on 2026-10-19, for 730
hours a month and little traffic. They are an estimate, not a quote. Startup credits would cover
them; the budget tracks the cost before credits.

| Part | Phase | Staging (live) | Production (when built) |
|---|---|---|---|
| Vespa VM e2-standard-4 ($0.1474/h), 128 GB pd-ssd, 20 GB boot disk | 1 | ~$132 | ~$132 |
| Cloud SQL db-g1-small ($0.035/h), 10 GB SSD, backups | 1 | ~$28 | ~$30 |
| Load balancer forwarding rules (~$18), Cloud Armor policy and rule (~$7) | 1 | ~$25 | ~$27 |
| Cloud NAT for two VMs ($0.0014/h each), its address ($0.005/h), little data | 1 | ~$6 | ~$6 |
| Artifact Registry, Secret Manager, transfer and archive buckets, logs | 1 | ~$3 | ~$5 |
| Cloud Run: one warm instance (1 vCPU, 1 GiB), jobs every minute, Scheduler | 3 | ~$20 | ~$25 |
| Workbench: Cloud SQL db-f1-micro ($0.0105/h) and storage; service scales to zero | 4 | ~$9 | ~$9 |
| GPU VM n1-standard-4 + T4 ($0.559/h; ~$286 with sustained use), 128 GB disk | when quota | ~$299 | ~$299 |
| or GPU VM g2-standard-4 + L4 ($0.778/h), 128 GB disk | when quota | ~$581 | ~$581 |
| **Total without the GPU** | | **~$223** | **~$234** |
| **Total with the T4** | | **~$522** | **~$533** |
| **Total with the L4** | | **~$804** | **~$815** |

- **The corpus bucket** (18.4 GB, Autoclass, EU: about $0.50) stays billed to
  `open-alvary-ingestion` while it is used in place.
- **Parking.** Stopping the VMs leaves only their disks (about $24 for the engine, $13 for the GPU).
  Cloud SQL can be stopped (activation policy `NEVER`), which leaves its storage. The forwarding
  rules keep billing.
- **The sustained-use discount** applies on its own to N1 machines and their GPUs (up to 30% for a
  full month). E2 and G2 get none. Committed-use discounts would lower the VM rows further.

## Before the first apply (owner)

1. **The project.** `open-alvary-staging` is active and billed to the same billing account as
   today's production, so the same credit covers it. Only Google's default services are on there
   today. The apply switches on the rest (`main.tf`), Compute Engine among them.
   - On a fresh project the first apply may stop right after switching them on, with "Cloud
     Resource Manager API has not been used": the services are on, but Terraform's check of each
     ran before Cloud Resource Manager had spread. Plan again and apply. The plan then shows those
     services as tainted, "to destroy" and to add: destroying one only drops it from the state
     (`disable_on_destroy = false`), so nothing is turned off.
   - Turning on Compute Engine may create a `default` network unless the organisation skips it.
   - Check with `gcloud compute networks list --project open-alvary-staging`.
   - If `default` is there, delete its firewall rules, then the network itself:
     `gcloud compute firewall-rules list --filter=network=default`, then
     `gcloud compute networks delete default`. Nothing here uses it.
2. **GPU quota** is per project. `open-alvary-prod` has T4 and L4 quota in europe-west1;
   `open-alvary-staging`'s is unknown, and likely 0.
   - Once Compute Engine is on, read `NVIDIA_T4_GPUS` in
     `gcloud compute regions describe europe-west1 --project open-alvary-staging`, and the
     project's global `GPUS_ALL_REGIONS` in the console's quotas page.
   - Request 1 of each if either is 0.
   - Until both are granted, apply with `inference_enabled = false`. The service then runs on the
     lexical arms without a reranker, and says so in every pack.
3. **Settings.**
   - `cp env/staging.tfvars.example env/staging.tfvars` and fill it in.
   - Bootstrap the state bucket:
     `cd bootstrap && terraform init && terraform apply -var 'projects={staging="open-alvary-staging"}'`.
   - Write `staging.backend.hcl` from `backend.hcl.example` and the bootstrap's output.
4. **Grants outside the project.** The apply adds members to today's two buckets (and, with
   ingestion, Document AI's role in `open-alvary-ingestion`), so it must run as someone who may set
   IAM there. The owner may.
5. **Pins to check.**
   - `tei_image` must be a text-embeddings-inference release that lists Qwen3-Embedding (1.8 or
     later), in the build for the GPU: `turing-` for the T4, `89-` for the L4.
   - `inference_boot_image` names a Deep Learning VM family that Google retires over time. List the
     current ones with
     `gcloud compute images list --project deeplearning-platform-release --filter='family~common-cu.*ubuntu-2204'`.
     `common-cu129-ubuntu-2204-nvidia-580` was current on 2026-10-19.
   - `vespa_image` is the release the tests ran on.

## Phases

Each phase ends with a check, and nothing serves the public until phase 6.

**Order when taking over from a live system: 1, 2, then 5's databases, then 3, 4 and 6.** The restore
loads only into an empty database, and both the first release's migrations (phase 3) and the
workbench's start (phase 4, `workbench/store.py`) create tables. So, while the old system still
serves:
- build the restore image and apply with `restore_image` set (this also creates the workbench's
  database, before the workbench);
- dump and restore both databases as a rehearsal;
- then release the public service (its migrations run on the restored schema) and the workbench, and
  test both on real data.

At switchover the rehearsal's data is stale. Empty both databases and restore the final dumps (phase
5's rehearsal note), with the workbench scaled to zero so it cannot create tables first.

1. **Foundation.** In `env/`:
   `terraform init -backend-config=../staging.backend.hcl`, then
   `terraform apply -var-file=staging.tfvars`, with `public_image` empty.
   - This creates the APIs, network, NAT, Cloud SQL, buckets, secret containers, registry, the load
     balancer's address and certificates, Cloud Armor, alerts, the budget, Vespa and the GPU machine.
   - Add the certificate CNAMEs that `terraform output dns_records` prints. They change nothing for
     visitors, and the certificates are issued while today's system keeps serving.
   - Check that the retrieval machines answer from inside the network, for example:
     `gcloud compute ssh open-alvary-staging-engine --tunnel-through-iap --command 'curl -s http://10.60.8.4:19071/state/v1/health'`
     (8080 answers once the retrieval job has deployed the application, in phase 3). On the GPU
     machine, run `nvidia-smi`, then `curl` `/health` on 8080 and 8081 at `10.60.8.36`.
2. **Secrets.** `./secrets.sh plan staging`, then `./secrets.sh copy staging`. Values go from today's
   Secret Manager (or, with `azure`, from Key Vault) through a pipe, never printed or written to disk.
3. **Public service.**
   - `./release.sh staging public` builds the image from the commit with Cloud Build. The first time,
     it prints the targeted apply that creates the migrations job; run it, then run the script again.
   - Set `public_image` and apply.
   - Check through the load balancer before DNS moves:
     `curl --resolve open.alvary.ai:443:<edge_address> https://open.alvary.ai/healthz`. Then the
     website and the full test and smoke suites, as `deployment/gcp/public_service.sh verify` does.
   - Check that the service sees each visitor's own address: second from the right in
     `X-Forwarded-For`, as today (`trusted_proxies = 2` in the admission configurations).
   - Check that the service has no run.app address of its own, and that Cloud Armor answers 429
     above 600 requests a minute on `/site/`.
4. **Workbench.**
   - Set `workbench_image`, `workbench_iap_members` and `ALVARY_REVIEW_OIDC_CLIENT_ID` in
     `workbench_settings`, then apply.
   - The workbench's address is new (`terraform output workbench_url`). Add `<address>/auth/callback`
     to its sign-in's OAuth client.
   - For Google accounts outside the organisation, give IAP the same client, as
     `deployment/gcp/workbench.sh iap_external` does (`gcloud iap settings set`, with
     `--resource-type=cloud-run`). The client secret is kept out of Terraform's state.
   - Check that an operator signs in and reviews a page.
5. **Data** (`./data.sh plan staging` lists it).
   - Build the restore and ingestion images (`./release.sh staging restore`, `./release.sh staging
     ingestion`), set `restore_image`, `ingestion_image` and `document_ai`, and apply.
   - Databases, in the switchover window with writes frozen:
     - `cloud-sql-proxy open-alvary-prod:europe-west1:open-alvary-db --port 55432`;
     - `./data.sh dump staging public`, then `./data.sh restore staging public`;
     - the same with `review` (55433, `open-alvary-ingestion:europe-west1:review-db`);
     - then `./release.sh staging public` runs the migrations against the restored schema.
     The restore recreates the source's roles (only the access role logs in), leaves Cloud SQL's
     own roles to Cloud SQL, loads the dump without owners, and fails on any table whose rows differ
     from the source's.
   - To rehearse first, while today's system still serves: dump and restore as above, check, then
     empty the target (`gcloud sql databases delete open_alvary --instance open-alvary-staging-pg`,
     then apply to make it again). The restore only loads into an empty database.
   - Buckets: today's are used in place, so there is nothing to copy (`./data.sh buckets staging`
     says so). An environment with its own buckets copies them with that command, after the owner
     grants `terraform output ingestion_identity` read on today's buckets. `./data.sh verify`
     checks every MD5.
   - Check: the restores pass, and the benchmarks give the same metrics through the new service.
6. **Switchover.**
   - Point the A records of `open.alvary.ai` and `api.open.alvary.ai` at `edge_address`.
   - In the same change, set `domain_live = true` and apply. The uptime check and alert start, and
     so do the worker's and daily job's schedules.
   - Pause the schedules in the environment being left, for example
     `gcloud scheduler jobs pause open-alvary-worker-every-5-minutes --location europe-west1 --project open-alvary-prod`,
     and `open-alvary-daily-sweep`.
   - Keep the old environment read-only as the fallback. Moving the A records back and resuming its
     schedules is the rollback.

## Moving back from Azure

The application already speaks both `gs://` and `az://`, and the restore job serves both clouds.
If Google Cloud was shut down in the meantime, today's buckets and Document AI processors went with
`open-alvary-ingestion`. In that case, leave `existing_corpus_bucket` and `existing_review_bucket`
empty, so the copies fill buckets of the environment's own, and make new processors with the same
pretrained versions (`document_ai`). The order is phase 1 above, then:

1. **Secrets.** `./secrets.sh copy staging azure`, in place of phase 2. It reads the live Azure
   environment's Key Vault (`AZURE_VAULT`, default `open-alvary-prod-sc-kv`). The names are the
   same. The key pepper and the feed and webhook keys carry over unchanged, so every API key, feed
   token and webhook keeps working. Then phases 3 and 4.
2. **Freeze writes on Azure** (the switchover window).
3. **Databases.** Azure's servers have no public address and cannot be given one (VNet-integrated
   servers keep their networking), so the dump is made inside Azure by its own restore job.
   - Once, give that job's identity write on Azure's transfer container. Azure's code grants it
     read only:
     `az role assignment create --assignee <public identity's principal ID> --role "Storage Blob Data Contributor" --scope <transfer container ID>`.
     Do the same for the workbench identity, for `review`.
   - `./data.sh dump staging public azure`. Azure's job runs `restore.py dump` and writes the
     database, its roles and its counts to Azure's transfer container. The script carries the three
     files through a private temporary folder to `gs://open-alvary-staging-transfer/public/`
     (Azure's storage must admit this machine's address: `operator_ip_ranges` there).
   - `./data.sh restore staging public`, the same for `review`, then `./release.sh staging public`
     for the migrations.
4. **Buckets.** Azure's copy job writes them, as this project's ingestion account, with no key.
   - Set `azure_federation` from `terraform output ingestion_identity` in deployment/azure/env, and
     apply here.
   - Run the command `terraform output azure_credential_command` prints. Store the file as
     `google-wif-config` in Azure's Key Vault, replacing the one that names `open-alvary-ingestion`.
   - `./data.sh buckets staging azure` copies the corpus, the archive, and (when the collection is
     not used in place) the first collection, create-only. A second run copies only what is new.
     `./data.sh verify staging azure` checks every MD5.
5. **Switchover** as in phase 6, pausing Azure's worker and daily jobs. Lock the archive here once
   its copy checks out. Then remove `azure_federation` and apply.

## Moving from here to Azure

deployment/azure/README.md's phases hold, with three differences, because this environment's
databases have private addresses only:

- **Secrets:** run Azure's `secrets.sh` with `GCP_PROJECT` and `GCP_REVIEW_PROJECT` both set to this
  project.
- **Databases:** cloud-sql-proxy cannot reach a private-only instance from outside, so this
  environment's restore job makes the dump. For each database, `public` and `review`:
  - run `gcloud run jobs execute open-alvary-staging-restore-public --args=dump --wait`
    (`restore_image` set);
  - carry the three files from `gs://open-alvary-staging-transfer/public/` to Azure's transfer
    container under `public/`, through a private temporary folder (`gcloud storage cp`, then
    `az storage blob upload`);
  - run Azure's `data.sh restore`.
- **Buckets:** Azure's copy job reads today's buckets as before, used in place here. They are
  copied from wherever they live.

## Locking the archive (owner, irreversible)

After the archive's contents are checked, lock its retention: set `is_locked = true` in
`storage.tf`'s `google_storage_bucket.archive` and apply, or run
`gcloud storage buckets update gs://open-alvary-<env>-archive --lock-retention-period`. Once locked,
the retention period can only be extended, and no one, including the owner, can delete an archived
original before it ends. Originals with personal data go to the corpus bucket instead (CPR-05:
lawful erasure stays possible). The audit-seal log bucket is locked the same way once its first
seals are in (`monitoring.tf`).

## Files

- `bootstrap/`: each environment's state bucket, in its own project.
- `env/`: the environment.
  - `main.tf` (the project's APIs), `network.tf`, `monitoring.tf`, `secrets.tf`, `registry.tf`,
    `identity.tf`, `postgres.tf`, `storage.tf`;
  - `engine.tf` and `inference.tf`, with their cloud-init templates;
  - `cloud_run.tf`, `jobs.tf`, `edge.tf`, `workbench.tf`, `ingestion.tf`, `transfer.tf`;
  - `variables.tf`, `locals.tf`, `outputs.tf`, `staging.tfvars.example` and `prod.tfvars.example`.
- `secrets.sh`: copies secrets from today's Secret Manager or from Azure's Key Vault.
- `release.sh` and `cloudbuild.yaml`: build an image from the commit, run the migrations, and give
  the image to apply.
- `data.sh`: the database dumps and restores and the bucket copies, with their checks. The restore
  job is deployment/azure/restore.py, built from deployment/azure/restore.Dockerfile; the copy is
  `operations/transfer.py`.
