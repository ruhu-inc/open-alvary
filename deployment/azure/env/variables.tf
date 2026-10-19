# --- where -----------------------------------------------------------------------------------------

variable "subscription_id" {
  description = "The Azure subscription (ruhu-sub): Open Alvary has its own resource group, access and budget in it."
  type        = string
}

variable "environment" {
  description = "Environment slug, as Ruhu's other projects: open-alvary-<env>-sc-rg. staging or prod."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "location" {
  description = "The primary region: Sweden Central, beside the services that call Open Alvary (EU)."
  type        = string
  default     = "swedencentral"
}

variable "operator_email" {
  description = "Where alerts and budget warnings go."
  type        = string
}

variable "operator_ip_ranges" {
  description = "Addresses (CIDR) from which the operator's Terraform and scripts reach Key Vault and storage."
  type        = list(string)
  default     = []
}

variable "budget_enabled" {
  description = "Cost Management budgets: only where the subscription supports them (not Sponsorship offers such as ruhu-sub)."
  type        = bool
  default     = false
}

variable "monthly_budget" {
  description = "The environment's monthly budget in USD, with budget_enabled: warnings at 50%, 80% and 100% (actual) and 100% (forecast)."
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
  description = "The public service image in this environment's registry, by commit tag (deployment/azure/release.sh). Empty until the first release: the apps are created only with an image."
  type        = string
  default     = ""
}

variable "feed_key_id" {
  description = "The key in feed-keys that seals new feed tokens (as on Google Cloud)."
  type        = string
  default     = "k1"
}

variable "webhook_key_id" {
  description = "The key in webhook-keys that seals new webhook secrets (as on Google Cloud)."
  type        = string
  default     = "w1"
}

variable "public_settings" {
  description = "PUBLIC_REPOSITORY_URL, PUBLIC_MAINTAINER, PUBLIC_CONTACT_EMAIL and METADATA_LICENCE, as on Google Cloud."
  type        = map(string)
}

variable "public_min_replicas" {
  type    = number
  default = 1 # one kept warm, as on Cloud Run
}

variable "public_max_replicas" {
  type    = number
  default = 3
}

variable "accounts_enabled" {
  description = "Activation B (MAC-15): accounts, the console, REST v1 and MCP, with their secrets and jobs."
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
  description = "Whether public_domain points at this environment's Front Door: the availability test and its alert run only then."
  type        = bool
  default     = false
}

variable "edge_requests_per_minute" {
  description = "Per-address limit on the website's paths at the edge (D23), as Cloud Armor had it."
  type        = number
  default     = 600
}

# --- retrieval: the engine and the models -----------------------------------------------------------

variable "engine_enabled" {
  description = "The Vespa machine. Off while the environment stands by (MAC-12a): the service then searches in PostgreSQL. On, the retrieval job deploys the application and feeds it from PostgreSQL within minutes."
  type        = bool
  default     = true
}

variable "engine_vm_size" {
  description = "Vespa, one node to start (the owner's choice): 4 vCPU and 16 GiB hold the current corpus."
  type        = string
  default     = "Standard_D4s_v5"
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
  description = "The GPU machine for the embedder and reranker. Off until the region grants GPU quota: the service then runs on the lexical arms without a reranker, and says so in every pack."
  type        = bool
  default     = true
}

variable "inference_vm_size" {
  description = "One GPU for the embedder and the reranker (NCasT4_v3: one T4, 16 GB). Needs GPU quota in the region."
  type        = string
  default     = "Standard_NC4as_T4_v3"
}

variable "tei_image" {
  description = "Hugging Face text-embeddings-inference for Turing GPUs (T4), at a release that serves Qwen3-Embedding."
  type        = string
  default     = "ghcr.io/huggingface/text-embeddings-inference:turing-1.8"
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

variable "admin_ssh_public_key" {
  description = "The SSH key for the engine and inference machines' admin account (they have no public address)."
  type        = string
}

# --- data ----------------------------------------------------------------------------------------------

variable "postgres_version" {
  description = "As on Cloud SQL (POSTGRES_16), so the database moves by dump and restore."
  type        = string
  default     = "16"
}

variable "postgres_sku_name" {
  type    = string
  default = "B_Standard_B2s"
}

variable "postgres_storage_mb" {
  type    = number
  default = 32768
}

variable "postgres_backup_retention_days" {
  type    = number
  default = 7
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

variable "workbench_domain" {
  description = "The workbench's own domain, such as review.open.alvary.ai."
  type        = string
  default     = ""
}

variable "workbench_settings" {
  description = "ALVARY_REVIEW_* settings that are not secrets (collections, allowed hosts, OIDC client ID, staging flag)."
  type        = map(string)
  default     = {}
}

variable "workbench_entra_client_id" {
  description = "The Entra app registration that admits operators to the workbench before its own sign-in (IAP's place)."
  type        = string
  default     = ""
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
  description = "ALVARY_DOCAI_PROJECT, _LOCATION, _PROCESSOR_ID, _PROCESSOR_VERSION, _OCR_PROCESSOR_ID and _OCR_PROCESSOR_VERSION, as on Google Cloud."
  type        = map(string)
  default     = {}
}

variable "restore_image" {
  description = "The restore job's image (release.sh restore), set only while databases are copied (phase 5)."
  type        = string
  default     = ""
}

variable "google_audience" {
  description = "The Entra application (Application ID URI) the ingestion identity's token is for when Google exchanges it (README, Google from Azure)."
  type        = string
  default     = "api://open-alvary-google"
}
