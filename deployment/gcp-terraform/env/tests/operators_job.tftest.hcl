# The operators job (workbench.tf), planned with stand-in providers: no cloud is touched and nothing
# needs signing in. `terraform init -backend=false && terraform test`. Its "impact" and "carry" compare
# a new copy with the collections the workbench reads, so it reads them as the workbench does: without
# its first collection or its corpus copies, a reviewed document there looked gone (2026-11-06).

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "random" {}

override_resource {
  target          = google_service_account.workbench
  override_during = plan
  values          = { email = "workbench@example.org", member = "serviceAccount:workbench@example.org" }
}
override_resource {
  target          = google_compute_network.main
  override_during = plan
  values          = { id = "projects/open-alvary-example/global/networks/main" }
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
  public_settings   = {}
  # A copy other than the default, as an environment names after each preparation.
  workbench_settings = { ALVARY_REVIEW_COLLECTIONS = "gs://corpus/collections-new/rw/:gs://corpus/collections-new/ng/" }
}

run "reads_the_collections_as_the_workbench_does" {
  command = plan

  assert {
    condition = alltrue([
      for name in ["ALVARY_REVIEW_STAGING", "ALVARY_REVIEW_COLLECTIONS"] :
      one([
        for e in google_cloud_run_v2_job.operators[0].template[0].template[0].containers[0].env : e.value if e.name == name
      ]) == local.workbench_env[name]
    ])
    error_message = "The operators job names the workbench's first collection and its corpus copies."
  }
  assert {
    condition = (
      google_cloud_run_v2_job.operators[0].template[0].template[0].volumes[0].gcs[0].bucket == local.collection_bucket
      && google_cloud_run_v2_job.operators[0].template[0].template[0].containers[0].volume_mounts[0].mount_path == local.workbench_env["ALVARY_REVIEW_STAGING"]
    )
    error_message = "The operators job mounts the workbench's first collection where the workbench reads it."
  }
}
