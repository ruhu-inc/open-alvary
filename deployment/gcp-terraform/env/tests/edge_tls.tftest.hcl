# The public load balancer's TLS floor (edge.tf), planned with stand-in providers: no cloud is touched.
# `terraform init -backend=false && terraform test`.

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "random" {}

override_resource {
  target          = google_compute_ssl_policy.edge
  override_during = plan
  values          = { id = "projects/open-alvary-example/global/sslPolicies/open-alvary-staging-tls" }
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
}

run "tls_1_2_at_least" {
  command = plan

  assert {
    condition     = length(google_compute_ssl_policy.edge) == 1 && google_compute_ssl_policy.edge[0].min_tls_version == "TLS_1_2"
    error_message = "The public load balancer refuses TLS below 1.2."
  }
  assert {
    condition     = google_compute_ssl_policy.edge[0].profile == "MODERN"
    error_message = "It uses the MODERN profile's ciphers."
  }
  assert {
    condition     = google_compute_target_https_proxy.public[0].ssl_policy == google_compute_ssl_policy.edge[0].id
    error_message = "The HTTPS proxy uses that policy."
  }
}
