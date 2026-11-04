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

  # Made by Terraform from the new instance (postgres.tf); the first is the database owner's login, and
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

  # The restricted logins (rights/logins.py), each with what its workloads do and no more; the logins job
  # makes them from these URLs. The owner's login is then for the migrations alone. Each URL's password
  # is generated here and reaches the job only through the secret: a plan shows neither.
  login_roles = {
    service   = "open_alvary_service"
    jobs      = "open_alvary_jobs"
    retrieval = "open_alvary_retrieval"
    publish   = "open_alvary_publish"
  }
  database_logins = var.database_logins_ready ? {
    for login, role in local.login_roles : login => role if login != "publish" || local.publish_enabled
  } : {}
  # The login each workload signs in with once it is in database_logins_in_use, and the account that
  # reads the login's secret.
  workload_login = {
    service   = "service"
    worker    = "jobs"
    daily     = "jobs"
    terms     = "jobs"
    retrieval = "retrieval"
    indexer   = "retrieval"
    publish   = "publish"
  }
  login_reader = {
    service   = "public"
    jobs      = "jobs"
    retrieval = "retrieval"
    publish   = "publisher"
  }
  # Workload → the secret holding the URL it signs in with.
  database_secret = {
    for workload, login in local.workload_login :
    workload => contains(var.database_logins_in_use, workload) ? "database-url-${login}" : "database-url"
  }

  # The shared account keeps the owner's secrets until nothing on it signs in as the owner.
  on_shared_account  = ["service", "worker", "daily", "terms", "retrieval", "indexer", "migrate"]
  shared_needs_owner = length(setsubtract(local.on_shared_account, var.database_logins_in_use)) > 0
  owner_readers = concat(
    local.shared_needs_owner ? ["public"] : [],
    var.database_logins_ready ? ["migrator"] : [],
  )
  # What a job moved off the shared account reads beside its own login (jobs.tf).
  moved_job_readers = var.database_logins_ready ? {
    "webhook-keys"   = ["jobs"]
    "resend-api-key" = var.accounts_enabled ? ["jobs"] : []
    "auth0-deletion-client-id" = (
      contains(var.optional_secrets, "auth0-deletion-client-id") ? ["jobs"] : []
    )
    "auth0-deletion-client-secret" = (
      contains(var.optional_secrets, "auth0-deletion-client-secret") ? ["jobs"] : []
    )
  } : {}

  secret_readers = merge(
    {
      for name, readers in local.copied_secrets :
      name => concat(name == "access-role-password" ? local.owner_readers : readers, lookup(local.moved_job_readers, name, []))
    },
    { "database-url" = local.owner_readers, "database-password" = local.owner_readers },
  )
  workload_accounts = merge(
    {
      public    = google_service_account.public.member
      workbench = google_service_account.workbench.member
    },
    var.database_logins_ready ? {
      migrator  = google_service_account.migrator[0].member
      jobs      = google_service_account.jobs[0].member
      retrieval = google_service_account.retrieval[0].member
    } : {},
    local.publish_enabled ? { publisher = google_service_account.publisher[0].member } : {},
  )
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

# Each restricted login's URL, its password generated here (letters and digits: rights/logins.py writes
# it into the role). Read by the workloads that sign in with it and by the logins job.
resource "random_password" "database_login" {
  for_each = local.database_logins
  length   = 40
  special  = false
}

resource "google_secret_manager_secret" "database_login" {
  for_each  = local.database_logins
  secret_id = "database-url-${each.key}"

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_secret_manager_secret_version" "database_login" {
  for_each = local.database_logins
  secret   = google_secret_manager_secret.database_login[each.key].id
  secret_data = format(
    "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
    each.value,
    random_password.database_login[each.key].result,
    google_sql_database_instance.main.private_ip_address,
    local.database,
  )
}

resource "google_secret_manager_secret_iam_member" "database_login" {
  for_each = {
    for pair in flatten([
      for login in keys(local.database_logins) : [
        for reader in distinct(["migrator", local.login_reader[login]]) : { login = login, reader = reader }
      ]
    ]) : "${pair.login}/${pair.reader}" => pair
  }
  secret_id = google_secret_manager_secret.database_login[each.value.login].id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.workload_accounts[each.value.reader]
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
