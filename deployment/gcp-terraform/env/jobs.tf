# The public service's jobs, on its image, identity, secrets and settings, as Cloud Run jobs started
# by Cloud Scheduler:
# - migrate: launch checks, migrations and the seed, run by release.sh before each new revision;
# - retrieval: every minute, the engine catches up with the change log, embedding jobs run and the
#   section index is rebuilt when its analyzer changed (python -m retrieval.worker run);
# - worker and daily (activation B): monitors, digests and webhooks every five minutes; audit seals,
#   sweeps and purges once a day at 03:15 UTC, as today. Their schedules stay paused until the
#   public domains point here (domain_live): only the live environment writes to the outside world.

locals {
  jobs = merge(
    {
      migrate = {
        command = ["sh", "-c", "python -m scripts.check_launch && alembic upgrade head && python -m scripts.seed"]
        cron    = null
        timeout = 900
        secrets = { POSTGRES_PASSWORD = "database-password" } # scripts.check_launch checks it
      }
      retrieval = {
        command = ["python", "-m", "retrieval.worker", "run"]
        cron    = "* * * * *"
        timeout = 55
        secrets = {}
      }
    },
    var.accounts_enabled ? {
      worker = {
        command = ["python", "-m", "changes.worker", "run"]
        cron    = "*/5 * * * *"
        timeout = 280
        secrets = {}
      }
      daily = {
        command = ["sh", "-c", "python -m accounts.seals seal && python -m changes.admin sweep && python -m changes.admin purge && python -m accounts.privacy purge && python -m accounts.seals verify"]
        cron    = "15 3 * * *"
        timeout = 600
        secrets = {}
      }
    } : {},
  )
  scheduled_jobs = { for name, job in local.jobs : name => job if job.cron != null }
}

resource "google_cloud_run_v2_job" "public" {
  for_each            = local.public_enabled ? local.jobs : {}
  name                = "${local.name_prefix}-${each.key}"
  location            = var.region
  deletion_protection = false

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.public.email
      timeout         = "${each.value.timeout}s"
      max_retries     = 0

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        name    = each.key
        image   = var.public_image
        command = each.value.command

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }

        dynamic "env" {
          for_each = local.public_env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = merge(local.public_secret_env, each.value.secrets)
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

        dynamic "volume_mounts" {
          for_each = local.public_secret_files
          content {
            name       = volume_mounts.key
            mount_path = "/secrets/${volume_mounts.key}"
          }
        }
      }

      dynamic "volumes" {
        for_each = local.public_secret_files
        content {
          name = volumes.key
          secret {
            secret = volumes.value
            items {
              path    = "config.json"
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # release.sh points the migrate job at a new image (gcloud) before Terraform does; gcloud also
    # stamps its own name here, which is no change worth applying.
    ignore_changes = [client, client_version]
  }

  depends_on = [google_secret_manager_secret_iam_member.readers, google_secret_manager_secret_version.generated]
}

# Cloud Scheduler starts each scheduled job through Cloud Run's API, as the scheduler account, which
# may start these jobs and nothing else.
resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  for_each = local.public_enabled ? local.scheduled_jobs : {}
  name     = google_cloud_run_v2_job.public[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

resource "google_cloud_scheduler_job" "public" {
  for_each         = local.public_enabled ? local.scheduled_jobs : {}
  name             = "${local.name_prefix}-${each.key}"
  region           = var.region
  schedule         = each.value.cron
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"
  paused           = contains(["worker", "daily"], each.key) && !var.domain_live

  retry_config {
    retry_count = 0 # the next run is never far off
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${google_cloud_run_v2_job.public[each.key].id}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler]
}
