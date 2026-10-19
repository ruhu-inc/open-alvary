# Cloud SQL PostgreSQL with a private address only (Private Service Access), in place of Azure's
# Flexible Server. The same major version (16) as on Azure and today's Cloud SQL, so the database
# moves by pg_dump and pg_restore (deployment/gcp-terraform/data.sh). The service connects over TCP
# to the private address, encrypted (ssl_mode and sslmode=require), not through the Cloud SQL
# connector's socket, which needs a public address. The user is open_alvary, as today; the
# migrations create the restricted roles (rights/sql/provision) inside.
#
# Extensions: Cloud SQL has no allow-list to set (Azure's azure.extensions). pg_trgm (catalogue
# search) and vector (the dense arm's fallback store) are both on its list of supported extensions,
# and open_alvary, a member of cloudsqlsuperuser, creates them when the migrations ask.

resource "random_password" "postgres" {
  length  = 40
  special = false # alphanumeric: what scripts.check_launch requires of POSTGRES_PASSWORD
}

resource "google_sql_database_instance" "main" {
  name                = "${local.name_prefix}-pg"
  region              = var.region
  database_version    = "POSTGRES_${var.postgres_version}"
  deletion_protection = var.deletion_protection

  settings {
    edition                     = "ENTERPRISE" # the shared-core tiers (db-f1-micro, db-g1-small) are Enterprise only
    tier                        = var.postgres_tier
    availability_type           = "ZONAL"
    disk_type                   = "PD_SSD"
    disk_size                   = var.postgres_disk_gb
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
      transaction_log_retention_days = var.postgres_backup_retention_days
      backup_retention_settings {
        retained_backups = var.postgres_backup_retention_days
      }
    }

    maintenance_window {
      day  = 7 # Sunday, 04:00 UTC: after the daily job, before Monday's traffic
      hour = 4
    }
  }

  depends_on = [google_service_networking_connection.services]
}

resource "google_sql_database" "main" {
  name     = local.database
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "main" {
  name            = "open_alvary"
  instance        = google_sql_database_instance.main.name
  password        = random_password.postgres.result
  deletion_policy = "ABANDON" # it owns the schema: the user goes with the instance, never on its own
}
