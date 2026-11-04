# The public service's jobs, on its image, identity, secrets and settings, as Cloud Run jobs started
# by Cloud Scheduler:
# - migrate: launch checks, migrations and the seed, run by release.sh before each new revision;
# - retrieval: every minute, the engine catches up with the change log, embedding jobs run and the
#   section index is rebuilt when its analyzer changed (python -m retrieval.worker run);
# - indexer: the semantic index's operator commands, run by hand (python -m retrieval.indexer);
# - worker and daily (activation B): monitors, digests and webhooks every five minutes; audit seals,
#   sweeps and purges once a day at 03:15 UTC, as today; terms, run by hand, emails owners of
#   new terms. Their schedules stay paused until the
#   public domains point here (domain_live): only the live environment writes to the outside world;
# - logins: makes or repairs the restricted database logins (rights/logins.py), run by hand.
#
# A job named in database_logins_in_use signs in with its own login, under its own account, with only
# the secrets it reads; until then it shares the service's login, account and secrets, as it always has.

locals {
  jobs = merge(
    {
      migrate = {
        # With the logins made, what a migration added reaches them before the new revision starts.
        command = ["sh", "-c", "python -m scripts.check_launch && alembic upgrade head && python -m scripts.seed${var.database_logins_ready ? " && python -m rights.logins grants" : ""}"]
        args    = []
        cron    = null
        timeout = 900
        secrets = { POSTGRES_PASSWORD = "database-password" } # scripts.check_launch checks it
        reads   = []
      }
    },
    # Off while the image is a release from before the retrieval service (retrieval.worker), such as
    # the one live when this environment took over.
    var.retrieval_enabled ? {
      retrieval = {
        command = ["python", "-m", "retrieval.worker", "run"]
        args    = []
        cron    = "* * * * *"
        timeout = 55
        secrets = {}
        reads   = []
      }
      # The semantic index's operator commands, run by hand with the arguments of each run (README,
      # "Semantic index"): prepare, validate, activate, rollback, purge, status.
      indexer = {
        command = ["python", "-m", "retrieval.indexer"]
        args    = ["status"]
        cron    = null
        timeout = 900
        secrets = {}
        reads   = []
      }
    } : {},
    var.accounts_enabled ? {
      worker = {
        command = ["python", "-m", "changes.worker", "run"]
        args    = []
        cron    = "*/5 * * * *"
        timeout = 280
        secrets = {}
        reads   = ["ALVARY_WEBHOOK_KEYS", "RESEND_API_KEY"]
      }
      daily = {
        command = ["sh", "-c", "python -m accounts.seals seal && python -m changes.admin sweep && python -m changes.admin purge && python -m accounts.privacy purge && python -m accounts.seals verify && python -m api.traffic maintain"]
        args    = []
        cron    = "15 3 * * *"
        timeout = 600
        secrets = {}
        reads   = ["ALVARY_AUTH0_DELETION_CLIENT_ID", "ALVARY_AUTH0_DELETION_CLIENT_SECRET"]
      }
      # New terms, emailed to organisations' owners (runbook, "New terms"), run by hand with the
      # operator and the notice's arguments. The command is fixed, so the job can do nothing else.
      terms = {
        command = ["sh", "-c", "exec python -m accounts.operator --operator \"$0\" terms-notice \"$@\""]
        args    = []
        cron    = null
        timeout = 900
        secrets = {}
        reads   = ["RESEND_API_KEY"]
      }
      # The workbench's database role and its grants (runbook, "Website traffic counts"), run by hand
      # after a migration that adds something the Access or Traffic section reads. Grants only: it has
      # no password to set, and the command is fixed.
      access-role = {
        command = ["python", "-m", "accounts.access_admin", "provision-role", "open_alvary_access"]
        args    = []
        cron    = null
        timeout = 300
        secrets = {}
        reads   = []
      }
    } : {},
    # Makes or repairs the restricted logins, safe to run again: with the argument check, signs in as each
    # and proves what it can and cannot do, changing nothing (the runbook's "Database logins").
    var.database_logins_ready ? {
      logins = {
        command = ["python", "-m", "rights.logins"]
        args    = ["provision"]
        cron    = null
        timeout = 300
        secrets = { for login in keys(local.database_logins) : "ALVARY_LOGIN_${upper(login)}_URL" => "database-url-${login}" }
        reads   = []
      }
    } : {},
  )
  scheduled_jobs = { for name, job in local.jobs : name => job if job.cron != null }

  # Moved off the shared account: the logins job always; access-role with the migrations, whose login it
  # shares; each other job when database_logins_in_use names it.
  moved_jobs = toset([
    for name in keys(local.jobs) : name
    if name == "logins" || contains(var.database_logins_in_use, name == "access-role" ? "migrate" : name)
  ])
  job_accounts = {
    migrate     = "migrator"
    access-role = "migrator"
    logins      = "migrator"
    retrieval   = "retrieval"
    indexer     = "retrieval"
    worker      = "jobs"
    daily       = "jobs"
    terms       = "jobs"
  }
  moved_accounts = var.database_logins_ready ? {
    migrator  = google_service_account.migrator[0].email
    jobs      = google_service_account.jobs[0].email
    retrieval = google_service_account.retrieval[0].email
  } : {}
  # Jobs read neither the admission configurations nor the service's other secrets.
  admission_settings = ["ALVARY_ADMISSION_CONFIG", "ALVARY_SITE_ADMISSION_CONFIG"]
  job_env = {
    for name in keys(local.jobs) : name => {
      for key, value in local.public_env : key => value
      if !contains(local.moved_jobs, name) || !contains(local.admission_settings, key)
    }
  }
  job_secret_env = {
    for name, job in local.jobs : name => merge(
      contains(local.moved_jobs, name) ? {
        for key, secret in local.public_secret_env : key => secret if contains(job.reads, key)
      } : local.public_secret_env,
      job.secrets,
      {
        DATABASE_URL = contains(keys(local.database_secret), name) ? local.database_secret[name] : "database-url"
      },
    )
  }
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
      service_account = contains(local.moved_jobs, each.key) ? local.moved_accounts[local.job_accounts[each.key]] : google_service_account.public.email
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
        args    = each.value.args

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }

        dynamic "env" {
          for_each = local.job_env[each.key]
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.job_secret_env[each.key]
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
          for_each = contains(local.moved_jobs, each.key) ? {} : local.public_secret_files
          content {
            name       = volume_mounts.key
            mount_path = "/secrets/${volume_mounts.key}"
          }
        }
      }

      dynamic "volumes" {
        for_each = contains(local.moved_jobs, each.key) ? {} : local.public_secret_files
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

  depends_on = [
    google_secret_manager_secret_iam_member.readers,
    google_secret_manager_secret_version.generated,
    google_secret_manager_secret_iam_member.database_login,
    google_secret_manager_secret_version.database_login,
  ]
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
