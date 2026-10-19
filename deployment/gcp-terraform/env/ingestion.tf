# The ingestion jobs (phase 5), created when ingestion_image is set: reading scanned pages and
# preparing them for review, run by hand (deployment/gcp-terraform/README.md), as
# deployment/gcp/ingestion_jobs.sh deploys them today. Reading is Google's Document AI, reached natively
# as the ingestion account (identity.tf): no key and no federation, unlike Azure's google-wif-config.
#
# transfer copies a store into this project's buckets (phase 5), and exists only when the environment
# has a corpus bucket of its own: by default today's corpus bucket into the new one; data.sh starts it
# with other arguments for the review collection and for verify. It reads the source as the ingestion
# account, so the source bucket's owner grants that account read (README, phase 5). With today's
# buckets used in place there is nothing to copy.

resource "google_cloud_run_v2_job" "ingestion" {
  for_each = var.ingestion_image == "" ? {} : merge(
    {
      scan    = { args = ["-m", "ingestion.scan_job"], env = { SCAN_READER = "ocr" }, timeout = 604800 } # 7 days, as today
      prepare = { args = ["-m", "ingestion.prepare_job"], env = {}, timeout = 86400 }
    },
    var.existing_corpus_bucket == "" ? {
      transfer = { args = ["-m", "operations.transfer", "copy", var.transfer_source, "gs://${local.corpus_bucket}"], env = {}, timeout = 86400 }
    } : {},
  )

  name                = "${local.name_prefix}-${each.key}"
  location            = var.region
  deletion_protection = false

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.ingestion.email
      timeout         = "${each.value.timeout}s"
      max_retries     = 1

      containers {
        name    = each.key
        image   = var.ingestion_image
        command = ["python"]
        args    = each.value.args

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        dynamic "env" {
          for_each = merge(
            { ALVARY_CORPUS_BUCKET = local.corpus_bucket },
            var.document_ai,
            each.value.env,
          )
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version] # gcloud's stamp when data.sh or an operator runs a job
  }

  depends_on = [google_storage_bucket_iam_member.corpus_write, google_project_iam_member.document_ai]
}
