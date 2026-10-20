# --- where -----------------------------------------------------------------------------------------

variable "project_id" {
  description = "The environment's own Google Cloud project, made fresh for it (README, Before the first apply)."
  type        = string
  validation {
    # Today's live projects were built by deployment/gcp/*.sh. Applying here would try to create
    # what already exists there, or replace it: never without an import plan (README).
    condition     = !contains(["open-alvary-prod", "open-alvary-ingestion"], var.project_id)
    error_message = "project_id names a live project built by deployment/gcp/*.sh: use a fresh project (README)."
  }
}

variable "environment" {
  description = "Environment slug: names are open-alvary-<env>-…. staging or prod."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "region" {
  description = "The primary region: Belgium, where Open Alvary has run on Google Cloud (EU)."
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "The zone of the engine and the GPU machine: one that offers the chosen GPU (L4: b and c; T4: b, c and d)."
  type        = string
  default     = "europe-west1-b"
}

variable "operator_email" {
  description = "Where alerts and budget warnings go."
  type        = string
}

variable "budget_enabled" {
  description = "A billing budget on this project. Needs billing_account and Billing Account Costs Manager (or Administrator) for whoever applies."
  type        = bool
  default     = false
}

variable "billing_account" {
  description = "The billing account the project is linked to (XXXXXX-XXXXXX-XXXXXX), for the budget."
  type        = string
  default     = ""
  validation {
    condition     = !var.budget_enabled || can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account))
    error_message = "budget_enabled needs billing_account, as XXXXXX-XXXXXX-XXXXXX."
  }
}

variable "monthly_budget" {
  description = "The environment's monthly budget in the billing account's currency, with budget_enabled: warnings at 50%, 80% and 100% (actual) and 100% (forecast)."
  type        = number
  default     = 0
}

# --- the public service ------------------------------------------------------------------------------

variable "public_domain" {
  description = "The website's domain, such as open.alvary.ai (staging: a staging name)."
  type        = string
}

variable "api_domain" {
  description = "The API's domain, such as api.open.alvary.ai."
  type        = string
}

variable "public_image" {
  description = "The public service image in this project's Artifact Registry, by commit tag (deployment/gcp-terraform/release.sh). Empty until the first release: the service and jobs are created only with an image."
  type        = string
  default     = ""
}

variable "feed_key_id" {
  description = "The key in feed-keys that seals new feed tokens (as on Google Cloud today)."
  type        = string
  default     = "k1"
}

variable "webhook_key_id" {
  description = "The key in webhook-keys that seals new webhook secrets (as on Google Cloud today)."
  type        = string
  default     = "w1"
}

variable "public_settings" {
  description = "PUBLIC_REPOSITORY_URL, PUBLIC_MAINTAINER, PUBLIC_CONTACT_EMAIL and METADATA_LICENCE, as on Google Cloud today."
  type        = map(string)
}

variable "public_min_replicas" {
  type    = number
  default = 1 # one kept warm, as today
}

variable "public_max_replicas" {
  description = "At most this many instances: admission-config's api_replicas and public_pool_size must cover them within the database's 50 connections."
  type        = number
  default     = 3
}

variable "accounts_enabled" {
  description = "Activation B (MAC-15): accounts, the console, REST v1 and MCP, with their secrets and jobs. On only in the environment that serves the public domains: its worker and daily jobs must run in one place (README, One cloud at a time)."
  type        = bool
  default     = false
}

variable "auth0_domain" {
  description = "The console's identity provider (Auth0), such as login.open.alvary.ai. Unchanged by the move."
  type        = string
  default     = ""
}

variable "console_client_id" {
  description = "The console's Auth0 client ID (public)."
  type        = string
  default     = ""
}

variable "domain_live" {
  description = "Whether public_domain points at this environment's load balancer: the uptime check and its alert run only then, and so do the worker's and the daily job's schedules, so an environment being readied beside the live one sends no digests or webhooks and purges nothing."
  type        = bool
  default     = false
}

variable "edge_requests_per_minute" {
  description = "Per-address limit on the website's paths at the edge (D23), as Cloud Armor has it today."
  type        = number
  default     = 600
}

# --- retrieval: the engine and the models -----------------------------------------------------------

variable "retrieval_enabled" {
  description = "The retrieval job (python -m retrieval.worker run, every minute). Off while public_image is a release from before the retrieval service: its module is not in that image."
  type        = bool
  default     = true
}

variable "engine_enabled" {
  description = "The Vespa machine. Off, the service runs without the engine (the lexical arms in PostgreSQL), and says so in every pack."
  type        = bool
  default     = true
}

variable "engine_vm_size" {
  description = "Vespa, one node to start (the owner's choice): a Compute Engine machine type with 4 vCPU and 16 GiB, which hold the current corpus."
  type        = string
  default     = "e2-standard-4"
}

variable "engine_disk_gb" {
  type    = number
  default = 128
}

variable "vespa_image" {
  description = "The Vespa image, pinned to the release the tests ran against."
  type        = string
  default     = "vespaengine/vespa:8.763.13"
}

variable "inference_enabled" {
  description = "The GPU machine for the embedder and reranker. Off until the project has GPU quota in the region: the service then runs on the lexical arms without a reranker, and says so in every pack."
  type        = bool
  default     = true
}

variable "inference_gpu" {
  description = "The GPU: t4 (n1-standard-4 with one NVIDIA T4, Turing; the owner's choice, as on Azure) or l4 (g2-standard-4, one NVIDIA L4, Ada). The project needs quota for it in the region."
  type        = string
  default     = "t4"
  validation {
    condition     = contains(["l4", "t4"], var.inference_gpu)
    error_message = "inference_gpu must be l4 or t4."
  }
}

variable "inference_vm_size" {
  description = "The GPU machine's type. Empty: g2-standard-4 for an L4, n1-standard-4 for a T4."
  type        = string
  default     = ""
}

variable "inference_boot_image" {
  description = "A Deep Learning VM image with NVIDIA's driver installed (Ubuntu 22.04). Families are retired over time: list the current ones with gcloud compute images list --project deeplearning-platform-release --filter='family~common-cu.*ubuntu-2204'."
  type        = string
  default     = "projects/deeplearning-platform-release/global/images/family/common-cu129-ubuntu-2204-nvidia-580"
}

variable "tei_image" {
  description = "Hugging Face text-embeddings-inference for the chosen GPU. Empty: turing-1.8 (Turing, for the T4; experimental upstream) or 89-1.8 (Ada, for the L4). Pin a release whose notes list Qwen3-Embedding (1.8 and later)."
  type        = string
  default     = ""
}

variable "embedder" {
  description = "The embedder D29 selected, pinned to the revision the screen served (experiments/embeddings/candidates.json)."
  type        = object({ model = string, revision = string })
  default     = { model = "Qwen/Qwen3-Embedding-0.6B", revision = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3" }
}

variable "reranker" {
  description = "The reranker D29 selected, pinned likewise."
  type        = object({ model = string, revision = string })
  default     = { model = "BAAI/bge-reranker-v2-m3", revision = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e" }
}

variable "vector_profile" {
  description = "The active vector profile the service searches (OPEN_ALVARY_VECTOR_PROFILE). Empty until its generation passes its gates."
  type        = string
  default     = ""
}

# --- data ----------------------------------------------------------------------------------------------

variable "postgres_version" {
  description = "As on Azure and today's Cloud SQL (16), so the database moves by dump and restore."
  type        = string
  default     = "16"
}

variable "postgres_tier" {
  description = "The public database's Cloud SQL tier. db-g1-small (shared core, 1.7 GB, 50 connections) is what production runs today."
  type        = string
  default     = "db-g1-small"
}

variable "postgres_disk_gb" {
  description = "The database's SSD, growing on demand."
  type        = number
  default     = 10
}

variable "postgres_backup_retention_days" {
  description = "Daily backups kept, and days of point-in-time recovery."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Cloud SQL's deletion protection, in Terraform and in Google's API. On for an environment that serves the public; off only to tear down a rehearsal."
  type        = bool
  default     = true
}

variable "existing_corpus_bucket" {
  description = "A corpus bucket to use in place (today's open-alvary-corpus, in open-alvary-ingestion), by name. Terraform then creates no corpus bucket and only grants this environment's accounts their roles on it; it never manages the bucket itself. Empty: a bucket of the environment's own."
  type        = string
  default     = ""
  validation {
    condition     = !startswith(var.existing_corpus_bucket, "gs://")
    error_message = "existing_corpus_bucket is a bucket's name, without gs://."
  }
}

variable "existing_review_bucket" {
  description = "The workbench's first collection in place (today's open-alvary-ingestion-review-collection), by name, likewise. Empty: a bucket of the environment's own."
  type        = string
  default     = ""
  validation {
    condition     = !startswith(var.existing_review_bucket, "gs://")
    error_message = "existing_review_bucket is a bucket's name, without gs://."
  }
}

variable "archive_retention_days" {
  description = "The archive's write-once retention (CPR-05). The policy is created unlocked; locking it is the owner's separate, irreversible step."
  type        = number
  default     = 3650
}

# --- the workbench and ingestion ------------------------------------------------------------------------

variable "workbench_image" {
  description = "The workbench image; empty until the workbench moves (phase 4)."
  type        = string
  default     = ""
}

variable "workbench_settings" {
  description = "ALVARY_REVIEW_* settings that are not secrets (OIDC client ID, IAP group, staging flag); they override the defaults in workbench.tf."
  type        = map(string)
  default     = {}
}

variable "workbench_iap_members" {
  description = "Who Identity-Aware Proxy lets through to the workbench's own sign-in, such as group:workbench-operators@ruhu.ai or user:someone@ruhu.ai (Azure's Entra ID app in IAP's place)."
  type        = list(string)
  default     = []
}

variable "ingestion_image" {
  description = "The ingestion jobs' image; empty until ingestion moves (phase 5)."
  type        = string
  default     = ""
}

variable "optional_secrets" {
  description = "Secrets the service reads when present: list the ones secrets.sh copied."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for name in var.optional_secrets : contains(["resend-webhook-secret", "auth0-deletion-client-id", "auth0-deletion-client-secret"], name)])
    error_message = "optional_secrets may name resend-webhook-secret, auth0-deletion-client-id and auth0-deletion-client-secret."
  }
}

variable "document_ai" {
  description = "ALVARY_DOCAI_PROJECT, _LOCATION, _PROCESSOR_ID, _PROCESSOR_VERSION, _OCR_PROCESSOR_ID and _OCR_PROCESSOR_VERSION, as today."
  type        = map(string)
  default     = {}
}

variable "restore_image" {
  description = "The restore job's image (release.sh restore), set only while databases are copied (phase 5)."
  type        = string
  default     = ""
}

variable "transfer_source" {
  description = "Where the transfer job copies the corpus from by default (only when the environment has a corpus bucket of its own): today's bucket. data.sh starts it with other arguments for the collection and for verify."
  type        = string
  default     = "gs://open-alvary-corpus"
}

variable "azure_federation" {
  description = "Moving back from Azure (README): Azure's ingestion identity (terraform output ingestion_identity there), admitted to act as this project's ingestion account, so Azure's copy job writes these buckets with no key. null otherwise."
  type = object({
    tenant_id    = string
    principal_id = string
    audience     = optional(string, "api://open-alvary-google")
  })
  default = null
}
