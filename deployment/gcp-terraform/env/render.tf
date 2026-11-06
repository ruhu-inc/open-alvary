# The page renderer (ingestion/render.py), created when render_image is set: pdftoppm draws the pages of
# outside PDFs for the workbench here, apart from everything the workbench holds. The service has an
# account of its own with no role, no secret, no setting and no volume. Its ingress is internal and only
# the workbench's account may invoke it, with an ID token. All its outbound traffic goes into a network
# of its own (Direct VPC egress, all traffic), which has no route out: no default route, no NAT, no
# Private Google Access, no peering, a rule refusing every outbound connection, and a resolver that
# forwards every name to an address where nothing answers. Cloud Run's own metadata server stays, as on
# every instance: it gives this account's tokens, for which no role was granted.
#
# The workbench reaches the service's run.app address through its own network: a private zone answers
# that one name with private.googleapis.com, which its subnet reaches by Private Google Access, so the
# request arrives as internal traffic. Cloud Run limits an HTTP/1 request to 32 MiB, so the container
# speaks HTTP/2 end to end (h2c), and a Gazette issue larger than that still goes through whole.

locals {
  render_enabled = var.render_image != ""
  render_name    = "${local.name_prefix}-render"
  # Cloud Run's deterministic address for the service, known before it exists.
  render_host = "${local.render_name}-${data.google_project.main.number}.${var.region}.run.app"
  # What the workbench is told (workbench.tf): pages are drawn by the service, and nowhere else.
  render_env = local.render_enabled ? {
    ALVARY_RENDER          = "service"
    ALVARY_RENDER_URL      = "https://${local.render_host}"
    ALVARY_RENDER_AUDIENCE = "https://${local.render_host}"
  } : {}
  # In no subnet of the renderer's network, which has no other route: nothing answers there.
  render_nowhere = "10.62.255.254"
}

resource "google_service_account" "render" {
  count        = local.render_enabled ? 1 : 0
  account_id   = local.render_name
  display_name = "Open Alvary page renderer, with no role (${var.environment})"

  depends_on = [google_project_service.main]
}

resource "google_compute_network" "render" {
  count                           = local.render_enabled ? 1 : 0
  name                            = "${local.render_name}-vpc"
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = true # no route to the internet's gateway

  depends_on = [google_project_service.main]
}

resource "google_compute_subnetwork" "render" {
  count                    = local.render_enabled ? 1 : 0
  name                     = local.render_name
  network                  = google_compute_network.render[0].id
  region                   = var.region
  ip_cidr_range            = local.cidr.render # Direct VPC egress takes addresses per instance
  private_ip_google_access = false             # no Google APIs either
}

resource "google_compute_firewall" "render_egress" {
  count              = local.render_enabled ? 1 : 0
  name               = "${local.render_name}-nothing-out"
  network            = google_compute_network.render[0].id
  direction          = "EGRESS"
  priority           = 0
  destination_ranges = ["0.0.0.0/0"]
  target_tags        = ["render"]

  deny {
    protocol = "all"
  }
}

resource "google_dns_policy" "render" {
  count                     = local.render_enabled ? 1 : 0
  name                      = "${local.render_name}-no-names"
  enable_inbound_forwarding = false
  enable_logging            = false

  alternative_name_server_config {
    target_name_servers {
      ipv4_address    = local.render_nowhere
      forwarding_path = "private"
    }
  }

  networks {
    network_url = google_compute_network.render[0].id
  }

  depends_on = [google_project_service.main]
}

resource "google_cloud_run_v2_service" "render" {
  count               = local.render_enabled ? 1 : 0
  name                = local.render_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account                  = google_service_account.render[0].email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    max_instance_request_concurrency = 1 # one pdftoppm per instance, within its memory
    timeout                          = "180s"

    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.render[0].id
        subnetwork = google_compute_subnetwork.render[0].id
        tags       = ["render"]
      }
    }

    containers {
      name  = "render"
      image = var.render_image

      ports {
        name           = "h2c"
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
      }
    }
  }

  depends_on = [google_compute_firewall.render_egress, google_dns_policy.render]
}

resource "google_cloud_run_v2_service_iam_member" "render" {
  count    = local.render_enabled ? 1 : 0
  name     = google_cloud_run_v2_service.render[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.workbench.member
}

resource "google_dns_managed_zone" "render" {
  count       = local.render_enabled ? 1 : 0
  name        = local.render_name
  dns_name    = "${local.render_host}."
  description = "The page renderer's address, answered inside the network so the workbench's requests stay internal"
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.main.id
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_dns_record_set" "render" {
  count        = local.render_enabled ? 1 : 0
  managed_zone = google_dns_managed_zone.render[0].name
  name         = google_dns_managed_zone.render[0].dns_name
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"] # private.googleapis.com
}
