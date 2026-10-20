# The database copy (phase 5): data.sh puts a dump, the source's roles and its row counts in the
# transfer bucket; a restore job inside the network loads them into the new, empty instance and
# compares the counts (deployment/azure/restore.py, whose image release.sh builds from
# deployment/azure/restore.Dockerfile: the same job serves both clouds). Run with the argument dump, the
# same job writes its own database out to the bucket instead (README, "Moving back from Azure" and
# away again). Everything in the bucket is deleted after 14 days. The restore jobs exist only while
# restore_image is set.

resource "google_storage_bucket" "transfer" {
  name                        = local.buckets.transfer
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # holds only copies, each deleted after 14 days

  soft_delete_policy {
    retention_duration_seconds = 0 # a dump deleted is gone: personal data is not kept beyond the 14 days
  }

  lifecycle_rule {
    condition {
      age = 14
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.main]
}

# Each restore job reads (and, for dump, writes) only its own database's folder.
resource "google_storage_bucket_iam_member" "transfer" {
  for_each = { public = google_service_account.public.member, workbench = google_service_account.workbench.member }
  bucket   = google_storage_bucket.transfer.name
  role     = "roles/storage.objectUser"
  member   = each.value
  condition {
    title      = "own-folder"
    expression = "resource.name.startsWith(\"projects/_/buckets/${local.buckets.transfer}/objects/${each.key == "public" ? "public" : "review"}/\")"
  }
}

locals {
  restores = var.restore_image == "" ? {} : merge(
    {
      public = {
        account = google_service_account.public.email
        secrets = { DATABASE_URL = "database-url", ACCESS_ROLE_PASSWORD = "access-role-password" }
      }
    },
    local.review_database_enabled ? {
      review = {
        account = google_service_account.workbench.email
        secrets = { DATABASE_URL = "review-database-url" }
      }
    } : {},
  )
}

resource "google_cloud_run_v2_job" "restore" {
  for_each            = local.restores
  name                = "${local.name_prefix}-restore-${each.key}"
  location            = var.region
  deletion_protection = false

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = each.value.account
      timeout         = "3600s"
      max_retries     = 0

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        name  = "restore"
        image = var.restore_image

        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }

        env {
          # data.sh puts each database's files here, replacing the last ones.
          name  = "TRANSFER"
          value = "gs://${google_storage_bucket.transfer.name}/${each.key}/"
        }
        dynamic "env" {
          for_each = each.value.secrets
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.readers,
    google_secret_manager_secret_iam_member.review_database_url,
    google_storage_bucket_iam_member.transfer,
  ]
}
