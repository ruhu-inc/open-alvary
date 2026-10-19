locals {
  project     = "open-alvary"
  environment = var.environment

  # The same naming as Ruhu's other Azure projects: <project>-<environment>-<region>-<kind>.
  region_short = lookup({
    "swedencentral"    = "sc"
    "southafricanorth" = "san"
    "westeurope"       = "weu"
  }, var.location, replace(var.location, "-", ""))

  # open-alvary-staging-sc → resource group open-alvary-staging-sc-rg, server open-alvary-staging-sc-pg.
  name_prefix = "${local.project}-${local.environment}-${local.region_short}"

  # Names capped at 24 or 32 characters (Key Vault, Container App jobs) use the short environment.
  storage_env  = local.environment == "staging" ? "stg" : local.environment
  short_prefix = "${local.project}-${local.storage_env}-${local.region_short}"

  # Storage account names: 3-24 lowercase letters and digits, unique in Azure.
  storage_suffix  = substr(sha1(var.subscription_id), 0, 4)
  storage_corpus  = "openalvary${local.storage_env}${local.region_short}corp${local.storage_suffix}"
  storage_archive = "openalvary${local.storage_env}${local.region_short}arch${local.storage_suffix}"
  registry_name   = "openalvary${local.storage_env}${local.region_short}acr${local.storage_suffix}"

  common_tags = {
    Project     = local.project
    Environment = local.environment
    ManagedBy   = "terraform"
    Owner       = "infrastructure"
  }

  # The network: Container Apps (delegated), PostgreSQL (delegated), the engine and the GPU machine.
  cidr = {
    vnet      = "10.50.0.0/16"
    apps      = "10.50.0.0/21"
    data      = "10.50.8.0/24"
    engine    = "10.50.9.0/27"
    inference = "10.50.9.32/27"
  }
  engine_ip    = cidrhost(local.cidr.engine, 4)
  inference_ip = cidrhost(local.cidr.inference, 4)

  database = "open_alvary"

  # What the retrieval service is told (retrieval/engine.py, retrieval/vectors.py, retrieval/rerank.py).
  retrieval_env = merge(
    {
      OPEN_ALVARY_ENGINE_URL        = "http://${local.engine_ip}:8080"
      OPEN_ALVARY_ENGINE_CONFIG_URL = "http://${local.engine_ip}:19071"
      OPEN_ALVARY_EMBEDDER_URL      = "http://${local.inference_ip}:8080/v1"
      OPEN_ALVARY_RERANKER_URL      = "http://${local.inference_ip}:8081"
      OPEN_ALVARY_RERANKER_PROFILE  = "${var.reranker.model}@${substr(var.reranker.revision, 0, 12)}"
    },
    var.vector_profile == "" ? {} : { OPEN_ALVARY_VECTOR_PROFILE = var.vector_profile },
  )
}
