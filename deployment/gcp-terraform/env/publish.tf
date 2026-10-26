# Publishing (operations.publish run): the publish job copies approved documents from the review
# workbench into the public catalogue, every night and whenever an administrator asks from the
# workbench's Publishing page (after a passkey check). Approval stays in the workbench; only this job
# writes approved documents into public view, and only within the owner's decisions for each country.
#
# It runs on the workbench's image (the review logic it applies is the workbench's) as an account of
# its own, which:
# - reads the review database with its own login, review_publisher, which the workbench makes when it
#   starts (workbench/store.py: provision_publisher): approvals, details, page revisions and operators'
#   names, read only, and the publishing tables, where it records each run; never sessions, passkeys
#   or enrolment codes, and never a review;
# - writes the public catalogue with the public service's database URL;
# - reads the collections (the first mounted read-only, the corpus ones from the corpus bucket).
# Cloud Scheduler starts it nightly; the workbench's account may start it and nothing else.

locals {
  publish_enabled = local.workbench_enabled && local.public_enabled
  publish_job     = "${local.name_prefix}-publish"
  publish_cron    = "30 1 * * *" # 01:30 UTC: 02:30 in Lagos, 03:30 in Kigali; the daily job runs at 03:15
  publish_words   = "every night at 01:30 UTC (02:30 in Lagos, 03:30 in Kigali)"
  # The job's full name, known before it exists (the workbench starts it by this name).
  publish_job_id = "projects/${var.project_id}/locations/${var.region}/jobs/${local.publish_job}"
}

resource "google_service_account" "publisher" {
  count        = local.publish_enabled ? 1 : 0
  account_id   = "${local.name_prefix}-publisher"
  display_name = "Open Alvary publish job (${var.environment})"

  depends_on = [google_project_service.main]
}

resource "random_password" "review_publisher" {
  count   = local.publish_enabled ? 1 : 0
  length  = 40
  special = false # letters and digits: workbench/store.py writes it into CREATE ROLE
}

resource "google_secret_manager_secret" "review_publisher_database_url" {
  count     = local.publish_enabled ? 1 : 0
  secret_id = "review-publisher-database-url"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_secret_manager_secret_version" "review_publisher_database_url" {
  count  = local.publish_enabled ? 1 : 0
  secret = google_secret_manager_secret.review_publisher_database_url[0].id
  secret_data = format(
    "postgresql+psycopg://review_publisher:%s@%s:5432/%s?sslmode=require",
    random_password.review_publisher[0].result,
    google_sql_database_instance.review[0].private_ip_address,
    google_sql_database.review[0].name,
  )
}

# The job signs in with it; the workbench makes the login with its password when it starts.
resource "google_secret_manager_secret_iam_member" "review_publisher_database_url" {
  for_each = local.publish_enabled ? {
    publisher = google_service_account.publisher[0].member
    workbench = google_service_account.workbench.member
  } : {}
  secret_id = google_secret_manager_secret.review_publisher_database_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}

resource "google_secret_manager_secret_iam_member" "publisher_public_database" {
  count     = local.publish_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.generated["database-url"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.publisher[0].member
}

resource "google_storage_bucket_iam_member" "publisher_reads" {
  for_each = local.publish_enabled ? { corpus = local.corpus_bucket, collection = local.collection_bucket } : {}
  bucket   = each.value
  role     = "roles/storage.objectViewer"
  member   = google_service_account.publisher[0].member
}

resource "google_cloud_run_v2_job" "publish" {
  count               = local.publish_enabled ? 1 : 0
  name                = local.publish_job
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account       = google_service_account.publisher[0].email
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2" # the collection mount needs it
      timeout               = "3600s"
      max_retries           = 0 # the next night, or Publish now, runs it again

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY" # both databases
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        name    = "publish"
        image   = var.workbench_image
        command = ["python", "-m", "operations.publish", "run"]

        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }

        dynamic "env" {
          for_each = {
            ALVARY_REVIEW_STAGING     = local.workbench_env["ALVARY_REVIEW_STAGING"]
            ALVARY_REVIEW_COLLECTIONS = local.workbench_env["ALVARY_REVIEW_COLLECTIONS"]
            ALVARY_REVIEW_CACHE       = "/tmp/review-cache"
            ALVARY_REVIEW_CACHE_MB    = "512"
          }
          content {
            name  = env.key
            value = env.value
          }
        }
        env {
          name = "ALVARY_REVIEW_DATABASE_URL" # the publisher's own login
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.review_publisher_database_url[0].secret_id
              version = "latest"
            }
          }
        }
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.generated["database-url"].secret_id
              version = "latest"
            }
          }
        }

        volume_mounts {
          name       = "collection"
          mount_path = "/data/collection"
        }
      }

      volumes {
        name = "collection"
        gcs {
          bucket    = local.collection_bucket
          read_only = true
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version]
  }

  depends_on = [
    google_secret_manager_secret_iam_member.review_publisher_database_url,
    google_secret_manager_secret_iam_member.publisher_public_database,
    google_secret_manager_secret_version.review_publisher_database_url,
    google_storage_bucket_iam_member.publisher_reads,
  ]
}

# Who may start it: the scheduler, nightly, and the workbench, when an administrator asks.
resource "google_cloud_run_v2_job_iam_member" "publish" {
  for_each = local.publish_enabled ? {
    scheduler = google_service_account.scheduler.member
    workbench = google_service_account.workbench.member
  } : {}
  name     = google_cloud_run_v2_job.publish[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value
}

resource "google_cloud_scheduler_job" "publish" {
  count            = local.publish_enabled ? 1 : 0
  name             = local.publish_job
  region           = var.region
  schedule         = local.publish_cron
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"

  retry_config {
    retry_count = 0 # the next night is a retry
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${google_cloud_run_v2_job.publish[0].id}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.publish]
}
