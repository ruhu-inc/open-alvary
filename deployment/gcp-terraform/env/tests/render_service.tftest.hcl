# The page renderer (render.tf), planned with stand-in providers: no cloud is touched and nothing needs
# signing in. `terraform init -backend=false && terraform test`. Off by default, nothing is made and the
# workbench draws pages as it did; on, the workbench draws them nowhere but the renderer, which holds
# nothing and reaches nothing.

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "random" {}

override_resource {
  target          = google_service_account.workbench
  override_during = plan
  values          = { email = "workbench@example.org", member = "serviceAccount:workbench@example.org" }
}
override_resource {
  target          = google_service_account.render
  override_during = plan
  values          = { email = "render@example.org", member = "serviceAccount:render@example.org" }
}
override_resource {
  target          = google_compute_network.main
  override_during = plan
  values          = { id = "projects/open-alvary-example/global/networks/main" }
}
override_resource {
  target          = google_compute_network.render
  override_during = plan
  values          = { id = "projects/open-alvary-example/global/networks/render" }
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

run "off_by_default" {
  command = plan

  assert {
    condition = (
      length(google_cloud_run_v2_service.render) == 0 && length(google_service_account.render) == 0
      && length(google_compute_network.render) == 0 && length(google_dns_managed_zone.render) == 0
      && length(google_dns_policy.render) == 0 && length(google_cloud_run_v2_service_iam_member.render) == 0
    )
    error_message = "Nothing of the renderer is made until render_image is set."
  }
  assert {
    condition     = !contains(local.services, "dns.googleapis.com")
    error_message = "No API is switched on for it."
  }
  assert {
    condition     = length([for name in keys(local.workbench_env) : name if startswith(name, "ALVARY_RENDER")]) == 0
    error_message = "The workbench is told nothing new: its image draws pages as before."
  }
}

run "on" {
  command = plan

  variables {
    render_image = "registry.example.org/render:1"
    # A setting that would draw pages in the workbench again is overruled.
    workbench_settings = { ALVARY_RENDER = "direct" }
  }

  assert {
    condition = (
      local.workbench_env["ALVARY_RENDER"] == "service"
      && contains(keys(local.workbench_env), "ALVARY_RENDER_URL")
      && contains(keys(local.workbench_env), "ALVARY_RENDER_AUDIENCE")
    )
    error_message = "The workbench draws pages at the renderer, with an ID token for it, and nowhere else."
  }
  assert {
    condition     = google_cloud_run_v2_service.render[0].ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY"
    error_message = "The renderer takes internal requests only."
  }
  assert {
    condition = (
      google_cloud_run_v2_service.render[0].template[0].service_account == "render@example.org"
      && length(google_cloud_run_v2_service.render[0].template[0].volumes) == 0
      && length(google_cloud_run_v2_service.render[0].template[0].containers[0].env) == 0
      && length(google_cloud_run_v2_service.render[0].template[0].containers[0].volume_mounts) == 0
    )
    error_message = "The renderer runs as its own account, with no setting, secret or volume."
  }
  assert {
    condition = (
      google_cloud_run_v2_service.render[0].template[0].vpc_access[0].egress == "ALL_TRAFFIC"
      && google_cloud_run_v2_service.render[0].template[0].vpc_access[0].network_interfaces[0].network == google_compute_network.render[0].id
      && google_cloud_run_v2_service.render[0].template[0].vpc_access[0].connector == null
    )
    error_message = "Everything the renderer sends goes into its own network, through no connector."
  }
  assert {
    condition = (
      google_compute_network.render[0].delete_default_routes_on_create
      && !google_compute_subnetwork.render[0].private_ip_google_access
      && google_compute_firewall.render_egress[0].direction == "EGRESS"
      && google_compute_firewall.render_egress[0].priority == 0
      && one(google_compute_firewall.render_egress[0].deny).protocol == "all"
    )
    error_message = "Its network has no route out, no Google APIs, and refuses every outbound connection first."
  }
  assert {
    condition     = google_cloud_run_v2_service_iam_member.render[0].member == "serviceAccount:workbench@example.org" && google_cloud_run_v2_service_iam_member.render[0].role == "roles/run.invoker"
    error_message = "Only the workbench's account may call it."
  }
  assert {
    condition = (
      google_dns_managed_zone.render[0].visibility == "private"
      && one(google_dns_managed_zone.render[0].private_visibility_config[0].networks).network_url == google_compute_network.main.id
      && one(google_dns_policy.render[0].networks).network_url == google_compute_network.render[0].id
    )
    error_message = "The workbench's network answers the renderer's name privately, and the renderer's own network has its resolver."
  }
  assert {
    condition     = contains(local.services, "dns.googleapis.com")
    error_message = "Cloud DNS is on for the zone and the renderer's resolver."
  }
}
