# The review workbench (phase 4), created when workbench_image is set. As today it has a Cloud SQL
# instance of its own, apart from the public service's, mounts its first collection read-only from
# the collection bucket (no copy at start: Cloud Run's /tmp is memory), reads the corpus collections
# from the corpus bucket (either may be today's bucket, used in place: storage.tf), and sits behind
# Identity-Aware Proxy on Cloud Run itself, as deployment/gcp/workbench.sh deploys it today: only
# workbench_iap_members reach the workbench's own sign-in, at its run.app address (Azure's Entra ID
# sign-in in IAP's place). That address is new with the project, so the sign-in's OAuth client must
# list its callback (README, phase 4).

locals {
  workbench_enabled = var.workbench_image != ""
  # The workbench's database comes before the workbench when the review data is restored into it: the
  # workbench creates its tables when it starts (workbench/store.py), and the restore loads only into
  # an empty database.
  review_database_enabled = local.workbench_enabled || var.restore_image != ""
  workbench_name          = "${local.name_prefix}-workbench"
  # Cloud Run's deterministic address for the service, known before it exists.
  workbench_host = "${local.workbench_name}-${data.google_project.main.number}.${var.region}.run.app"

  workbench_env = merge(
    {
      ALVARY_REVIEW_BASE_URL      = "https://${local.workbench_host}"
      ALVARY_REVIEW_ALLOWED_HOSTS = local.workbench_host
      ALVARY_REVIEW_STAGING       = "/data/collection"
      ALVARY_REVIEW_COLLECTIONS   = "gs://${local.corpus_bucket}/collections/rw/"
      ALVARY_REVIEW_CACHE         = "/tmp/review-cache"
      ALVARY_REVIEW_CACHE_MB      = "512" # /tmp is memory on Cloud Run: a quarter of the instance's 2 GiB
    },
    var.workbench_settings,
  )
  workbench_secret_env = merge(
    {
      ALVARY_REVIEW_DATABASE_URL       = "review-database-url"
      ALVARY_REVIEW_OIDC_CLIENT_SECRET = "review-oidc-client-secret"
    },
    var.accounts_enabled ? {
      ALVARY_ACCESS_DATABASE_URL = "access-database-url"
      RESEND_API_KEY             = "resend-api-key"
    } : {},
  )
}

resource "random_password" "review" {
  count   = local.review_database_enabled ? 1 : 0
  length  = 40
  special = false
}

resource "google_sql_database_instance" "review" {
  count               = local.review_database_enabled ? 1 : 0
  name                = "${local.name_prefix}-review-pg"
  region              = var.region
  database_version    = "POSTGRES_${var.postgres_version}"
  deletion_protection = var.deletion_protection

  settings {
    edition                     = "ENTERPRISE"
    tier                        = "db-f1-micro" # as today's review-db
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    deletion_protection_enabled = var.deletion_protection
    user_labels                 = local.common_labels

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.main.id
      ssl_mode                                      = "ENCRYPTED_ONLY"
      enable_private_path_for_google_cloud_services = true
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "02:00"
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 7
      }
    }
  }

  depends_on = [google_service_networking_connection.services]
}

# The names today's review-db uses (user and database review), so a backup of it restores here with
# Terraform's state still true (data.sh backup).
resource "google_sql_database" "review" {
  count    = local.review_database_enabled ? 1 : 0
  name     = "review"
  instance = google_sql_database_instance.review[0].name
}

resource "google_sql_user" "review" {
  count           = local.review_database_enabled ? 1 : 0
  name            = "review"
  instance        = google_sql_database_instance.review[0].name
  password        = random_password.review[0].result
  deletion_policy = "ABANDON"
}

resource "google_secret_manager_secret" "review_database_url" {
  count     = local.review_database_enabled ? 1 : 0
  secret_id = "review-database-url"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_secret_manager_secret_version" "review_database_url" {
  count  = local.review_database_enabled ? 1 : 0
  secret = google_secret_manager_secret.review_database_url[0].id
  secret_data = format(
    "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
    google_sql_user.review[0].name,
    random_password.review[0].result,
    google_sql_database_instance.review[0].private_ip_address,
    google_sql_database.review[0].name,
  )
}

resource "google_secret_manager_secret_iam_member" "review_database_url" {
  count     = local.review_database_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.review_database_url[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.workbench.member
}

resource "google_cloud_run_v2_service" "workbench" {
  count               = local.workbench_enabled ? 1 : 0
  name                = local.workbench_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true # IAP checks every request before it reaches the workbench
  deletion_protection = false

  template {
    service_account                  = google_service_account.workbench.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2" # a bucket mount needs it
    max_instance_request_concurrency = 80
    # A new database URL starts a new revision (cloud_run.tf).
    labels = { review-database-url-version = google_secret_manager_secret_version.review_database_url[0].version }

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY" # its database, and the public one for the Access section
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      name    = "workbench"
      image   = var.workbench_image
      command = ["uvicorn"]
      args    = ["workbench.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
      }

      dynamic "env" {
        for_each = local.workbench_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.workbench_secret_env
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

  depends_on = [
    google_secret_manager_secret_iam_member.readers,
    google_secret_manager_secret_iam_member.review_database_url,
    google_secret_manager_secret_version.review_database_url,
    google_storage_bucket_iam_member.collection_read,
  ]
}

# Identity-Aware Proxy calls the service as its own service agent, which must exist first.
resource "google_project_service_identity" "iap" {
  provider = google-beta
  count    = local.workbench_enabled ? 1 : 0
  project  = var.project_id
  service  = "iap.googleapis.com"

  depends_on = [google_project_service.main]
}

resource "google_cloud_run_v2_service_iam_member" "iap" {
  count    = local.workbench_enabled ? 1 : 0
  name     = google_cloud_run_v2_service.workbench[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap[0].email}"
}

resource "google_iap_web_cloud_run_service_iam_member" "operators" {
  for_each               = local.workbench_enabled ? toset(var.workbench_iap_members) : toset([])
  cloud_run_service_name = google_cloud_run_v2_service.workbench[0].name
  location               = var.region
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}

# Operators (workbench/admin.py): the workbench has no web route that adds people, by design. This job
# runs the same command against this environment's workbench database, with the arguments given at
# each run (README, "Workbench operators"):
#   gcloud run jobs execute <name> --region <region> --args=enrol,someone@example.org,--replace
# Its default, list, changes nothing. An enrolment code it prints (24 hours, one use) is in the job's
# log, readable by the project's owners only.
resource "google_cloud_run_v2_job" "operators" {
  count               = local.workbench_enabled ? 1 : 0
  name                = "${local.name_prefix}-operators"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.workbench.email
      timeout         = "120s"
      max_retries     = 0

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.main.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        name    = "operators"
        image   = var.workbench_image
        command = ["python", "-m", "workbench.admin"]
        args    = ["list"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name = "ALVARY_REVIEW_DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.review_database_url[0].secret_id
              version = "latest"
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
    google_secret_manager_secret_iam_member.review_database_url,
    google_secret_manager_secret_version.review_database_url,
  ]
}
