# The restricted database logins, planned with stand-in providers: no cloud is touched and nothing needs
# signing in. `terraform init -backend=false && terraform test`. By default nothing changes for the live
# system; each step of the runbook's "Database logins" moves only what it names.

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "random" {}

# The accounts' addresses, known at plan time so that each job's account can be compared.
override_resource {
  target          = google_service_account.public
  override_during = plan
  values          = { email = "public@example.org", member = "serviceAccount:public@example.org" }
}
override_resource {
  target          = google_service_account.migrator
  override_during = plan
  values          = { email = "migrator@example.org", member = "serviceAccount:migrator@example.org" }
}
override_resource {
  target          = google_service_account.jobs
  override_during = plan
  values          = { email = "jobs@example.org", member = "serviceAccount:jobs@example.org" }
}
override_resource {
  target          = google_service_account.retrieval
  override_during = plan
  values          = { email = "retrieval@example.org", member = "serviceAccount:retrieval@example.org" }
}

variables {
  project_id        = "open-alvary-example"
  environment       = "staging"
  operator_email    = "operator@example.org"
  public_domain     = "open.example.org"
  api_domain        = "api.open.example.org"
  public_image      = "registry.example.org/public:1"
  workbench_image   = "registry.example.org/workbench:1"
  accounts_enabled  = true
  auth0_domain      = "login.example.org"
  console_client_id = "console"
  optional_secrets  = ["auth0-deletion-client-id", "auth0-deletion-client-secret"]
  public_settings   = {}
}

run "off_by_default" {
  command = plan

  assert {
    condition     = length(google_secret_manager_secret.database_login) == 0 && length(random_password.database_login) == 0
    error_message = "No login's secret or password is made until database_logins_ready."
  }
  assert {
    condition     = !contains(keys(google_cloud_run_v2_job.public), "logins")
    error_message = "No logins job until database_logins_ready."
  }
  assert {
    condition     = length(google_service_account.migrator) == 0 && length(google_service_account.jobs) == 0 && length(google_service_account.retrieval) == 0
    error_message = "No new account until database_logins_ready."
  }
  assert {
    condition     = local.database_secret["service"] == "database-url" && local.database_secret["publish"] == "database-url"
    error_message = "The service and the publish job keep the login they have."
  }
  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.public :
      job.template[0].template[0].service_account == google_service_account.public.email
    ])
    error_message = "Every job keeps the shared account."
  }
  assert {
    condition     = alltrue([for name, env in local.job_secret_env : env["DATABASE_URL"] == "database-url" && contains(keys(env), "ALVARY_FEED_KEYS")])
    error_message = "Every job keeps the owner's login and the service's secrets."
  }
  assert {
    condition     = !strcontains(local.jobs["migrate"].command[2], "rights.logins")
    error_message = "The migrate job's command is as it was."
  }
  assert {
    condition     = contains(local.secret_readers["database-url"], "public") && length(local.secret_readers["database-url"]) == 1
    error_message = "The shared account alone reads the owner's login."
  }
}

run "ready_changes_nothing_in_use" {
  command = plan

  variables {
    database_logins_ready = true
  }

  assert {
    condition     = toset(keys(google_secret_manager_secret.database_login)) == toset(["service", "jobs", "retrieval", "publish"])
    error_message = "Each login has its secret."
  }
  assert {
    condition     = google_cloud_run_v2_job.public["logins"].template[0].template[0].service_account == google_service_account.migrator[0].email
    error_message = "The logins job runs under the migrations' account."
  }
  assert {
    condition     = local.job_secret_env["logins"]["DATABASE_URL"] == "database-url"
    error_message = "The logins job signs in as the owner."
  }
  assert {
    condition     = local.database_secret["service"] == "database-url"
    error_message = "The service stays on the login it has."
  }
  assert {
    condition = alltrue([
      for name, job in google_cloud_run_v2_job.public :
      job.template[0].template[0].service_account == google_service_account.public.email if name != "logins"
    ])
    error_message = "Every other job keeps the shared account."
  }
  assert {
    condition     = strcontains(local.jobs["migrate"].command[2], "python -m rights.logins grants")
    error_message = "A migration's new tables reach the logins."
  }
}

run "one_job_moves_alone" {
  command = plan

  variables {
    database_logins_ready  = true
    database_logins_in_use = ["retrieval"]
  }

  assert {
    condition     = google_cloud_run_v2_job.public["retrieval"].template[0].template[0].service_account == google_service_account.retrieval[0].email
    error_message = "The retrieval job has its own account."
  }
  assert {
    condition     = local.job_secret_env["retrieval"]["DATABASE_URL"] == "database-url-retrieval" && length(local.job_secret_env["retrieval"]) == 1
    error_message = "The retrieval job reads its own login and no other secret."
  }
  assert {
    condition     = local.job_secret_env["worker"]["DATABASE_URL"] == "database-url" && local.database_secret["service"] == "database-url"
    error_message = "Nothing else moves."
  }
  assert {
    condition     = contains(local.secret_readers["database-url"], "public")
    error_message = "The shared account keeps the owner's login while anything on it signs in with it."
  }
}

run "all_moved" {
  command = plan

  variables {
    database_logins_ready  = true
    database_logins_in_use = ["service", "worker", "daily", "terms", "retrieval", "indexer", "publish", "migrate"]
  }

  assert {
    condition     = join(",", local.secret_readers["database-url"]) == "migrator" && join(",", local.secret_readers["database-password"]) == "migrator"
    error_message = "Only the migrations' account reads the owner's login."
  }
  assert {
    condition     = join(",", local.secret_readers["access-role-password"]) == "migrator"
    error_message = "The access role's password goes with the owner's."
  }
  assert {
    condition     = local.database_secret["service"] == "database-url-service" && local.database_secret["publish"] == "database-url-publish"
    error_message = "The service and the publish job sign in with their own logins."
  }
  assert {
    condition     = google_cloud_run_v2_job.public["migrate"].template[0].template[0].service_account == google_service_account.migrator[0].email
    error_message = "The migrations run under their own account."
  }
  assert {
    condition     = local.job_secret_env["migrate"]["DATABASE_URL"] == "database-url" && !contains(keys(local.job_secret_env["migrate"]), "ALVARY_KEY_PEPPER")
    error_message = "The migrations keep the owner's login, and read no other secret."
  }
  assert {
    condition     = join(",", sort(keys(local.job_secret_env["worker"]))) == "ALVARY_WEBHOOK_KEYS,DATABASE_URL,RESEND_API_KEY"
    error_message = "The worker reads only what it uses."
  }
  assert {
    condition     = !contains(keys(local.job_env["worker"]), "ALVARY_ADMISSION_CONFIG")
    error_message = "A moved job does not point at the admission files it no longer mounts."
  }
  assert {
    condition     = join(",", local.secret_readers["webhook-keys"]) == "public,jobs"
    error_message = "The webhook keys are read by the service and the jobs' account."
  }
}
