locals {
  project     = "open-alvary"
  environment = var.environment

  # open-alvary-staging → network open-alvary-staging-vpc, database open-alvary-staging-pg. The
  # project itself says which environment it is, so no region is in the names (as on Azure's
  # <project>-<environment>-<region>-<kind>): there is one region. Every name fits Google's limits
  # (service accounts 30 characters, buckets 63), so no shorter form is needed.
  name_prefix = "${local.project}-${local.environment}"

  # Applied to every resource that takes labels (providers.tf).
  common_labels = {
    project     = local.project
    environment = local.environment
    managed-by  = "terraform"
    owner       = "infrastructure"
  }

  # Bucket names are global: the environment's prefix makes them its own.
  buckets = {
    corpus     = "${local.name_prefix}-corpus"
    collection = "${local.name_prefix}-review-collection"
    archive    = "${local.name_prefix}-archive"
    transfer   = "${local.name_prefix}-transfer"
  }

  # Where release.sh pushes images.
  registry = "${var.region}-docker.pkg.dev/${var.project_id}/${local.project}"

  # The network: Cloud Run's egress (the service, its jobs and the workbench), the engine, the GPU
  # machine, and the range Google peers Cloud SQL into (Private Service Access).
  cidr = {
    run       = "10.60.0.0/22"
    engine    = "10.60.8.0/27"
    inference = "10.60.8.32/27"
    services  = "10.60.16.0/20"
  }
  engine_ip    = cidrhost(local.cidr.engine, 4)
  inference_ip = cidrhost(local.cidr.inference, 4)

  database = "open_alvary"

  # The GPU machine for each GPU, and the text-embeddings-inference build for its architecture.
  gpu = {
    l4 = { machine = "g2-standard-4", accelerator = null, tei = "ghcr.io/huggingface/text-embeddings-inference:89-1.8" }
    t4 = { machine = "n1-standard-4", accelerator = "nvidia-tesla-t4", tei = "ghcr.io/huggingface/text-embeddings-inference:turing-1.8" }
  }[var.inference_gpu]
  inference_vm_size = var.inference_vm_size != "" ? var.inference_vm_size : local.gpu.machine
  tei_image         = var.tei_image != "" ? var.tei_image : local.gpu.tei

  # What the retrieval service is told (retrieval/engine.py, retrieval/vectors.py, retrieval/rerank.py):
  # the engine only when its machine exists, the models only when the GPU machine does.
  retrieval_env = merge(
    var.engine_enabled ? {
      OPEN_ALVARY_ENGINE_URL        = "http://${local.engine_ip}:8080"
      OPEN_ALVARY_ENGINE_CONFIG_URL = "http://${local.engine_ip}:19071"
    } : {},
    var.inference_enabled ? {
      OPEN_ALVARY_EMBEDDER_URL     = "http://${local.inference_ip}:8080/v1"
      OPEN_ALVARY_RERANKER_URL     = "http://${local.inference_ip}:8081"
      OPEN_ALVARY_RERANKER_PROFILE = "${var.reranker.model}@${substr(var.reranker.revision, 0, 12)}"
    } : {},
    var.vector_profile == "" || !var.inference_enabled ? {} : { OPEN_ALVARY_VECTOR_PROFILE = var.vector_profile },
  )
}
