# The application's secrets, in Secret Manager in place of Key Vault. Terraform makes every secret's
# container and the values it generates itself: the database URLs and password. Every other value is
# copied by deployment/gcp-terraform/secrets.sh, value for value, from today's Secret Manager or from
# Azure's Key Vault (the names are the same in all three), so API keys, feed tokens and webhook
# secrets keep working (the key pepper and the feed and webhook keys must never be regenerated).
# Those values never pass through Terraform or its state. Each secret is readable only by the
# workloads that use it, and the services read secrets by name, never by value.

locals {
  # Secret → the workloads that read it. Copied (or, access-database-url, composed) by secrets.sh.
  copied_secrets = merge(
    {
      for name in ["feed-keys", "webhook-keys", "admission-config", "site-admission-config", "access-role-password", "key-pepper", "consent-secret", "console-client-secret"] :
      name => ["public"]
    },
    { "resend-api-key" = var.accounts_enabled ? ["public", "workbench"] : ["public"] },
    { for name in var.optional_secrets : name => ["public"] },
    {
      "review-oidc-client-secret" = ["workbench"]
      "access-database-url"       = var.accounts_enabled ? ["workbench"] : []
    },
  )

  # Made by Terraform from the new instance (postgres.tf); the service connects with the first, and
  # the launch check reads the second (scripts.check_launch, in the migrate job).
  generated_secrets = {
    "database-url" = format(
      "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
      google_sql_user.main.name,
      random_password.postgres.result,
      google_sql_database_instance.main.private_ip_address,
      local.database,
    )
    "database-password" = random_password.postgres.result
  }

  secret_readers = merge(
    { for name, readers in local.copied_secrets : name => readers },
    { "database-url" = ["public"], "database-password" = ["public"] },
  )
  workload_accounts = {
    public    = google_service_account.public.member
    workbench = google_service_account.workbench.member
  }
}

resource "google_secret_manager_secret" "copied" {
  for_each  = local.copied_secrets
  secret_id = each.key

  replication {
    user_managed {
      replicas {
        location = var.region # kept in the region, as today
      }
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_secret_manager_secret" "generated" {
  for_each  = toset(keys(local.generated_secrets))
  secret_id = each.key

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_secret_manager_secret_version" "generated" {
  for_each    = toset(keys(local.generated_secrets))
  secret      = google_secret_manager_secret.generated[each.key].id
  secret_data = local.generated_secrets[each.key]
}

resource "google_secret_manager_secret_iam_member" "readers" {
  for_each = {
    for pair in flatten([
      for name, readers in local.secret_readers : [for reader in readers : { name = name, reader = reader }]
    ]) : "${pair.name}/${pair.reader}" => pair
  }
  secret_id = contains(keys(local.generated_secrets), each.value.name) ? google_secret_manager_secret.generated[each.value.name].id : google_secret_manager_secret.copied[each.value.name].id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.workload_accounts[each.value.reader]
}
