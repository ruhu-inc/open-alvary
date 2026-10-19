# One network. Cloud Run (the service, its jobs, the workbench) sends its private traffic into the run
# subnet by Direct VPC egress; Cloud SQL has a private address only, in the range Google peers into
# the network (Private Service Access); the engine and the GPU machine have private addresses only,
# reachable from the run subnet and nowhere else. A VPC network refuses every inbound connection it
# has no rule for, so the rules below are the whole of what reaches the machines (Azure's
# "nothing-else" rule is Google's implied one). Secret Manager and the buckets are guarded by IAM
# alone: they take no network rules (README, "Where Google Cloud differs").

resource "google_compute_network" "main" {
  name                    = "${local.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.main]
}

resource "google_compute_subnetwork" "run" {
  name                     = "${local.name_prefix}-run"
  network                  = google_compute_network.main.id
  region                   = var.region
  ip_cidr_range            = local.cidr.run # Direct VPC egress takes addresses per instance: room to scale
  private_ip_google_access = true
}

resource "google_compute_subnetwork" "engine" {
  name                     = "${local.name_prefix}-engine"
  network                  = google_compute_network.main.id
  region                   = var.region
  ip_cidr_range            = local.cidr.engine
  private_ip_google_access = true
}

resource "google_compute_subnetwork" "inference" {
  name                     = "${local.name_prefix}-inference"
  network                  = google_compute_network.main.id
  region                   = var.region
  ip_cidr_range            = local.cidr.inference
  private_ip_google_access = true
}

# Cloud SQL's private addresses come from this range, peered into the network.
resource "google_compute_global_address" "services" {
  name          = "${local.name_prefix}-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = cidrhost(local.cidr.services, 0)
  prefix_length = tonumber(split("/", local.cidr.services)[1])
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "services" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.services.name]
  deletion_policy         = "ABANDON" # Google removes the peering with the last instance, not before
}

# The engine answers queries (8080) and takes its application (19071) from the run subnet only.
resource "google_compute_firewall" "engine" {
  name          = "${local.name_prefix}-engine-from-run"
  network       = google_compute_network.main.id
  direction     = "INGRESS"
  source_ranges = [local.cidr.run]
  target_tags   = ["engine"]

  allow {
    protocol = "tcp"
    ports    = ["8080", "19071"]
  }
}

# The models answer embeddings (8080) and reranking (8081) from the run subnet only.
resource "google_compute_firewall" "inference" {
  name          = "${local.name_prefix}-inference-from-run"
  network       = google_compute_network.main.id
  direction     = "INGRESS"
  source_ranges = [local.cidr.run]
  target_tags   = ["inference"]

  allow {
    protocol = "tcp"
    ports    = ["8080", "8081"]
  }
}

# The operator's way onto the machines, which have no public address: SSH through Identity-Aware
# Proxy's TCP forwarding (gcloud compute ssh --tunnel-through-iap), signed in with OS Login, from
# IAP's own range only. Who may do it is IAM's (roles/iap.tunnelResourceAccessor; owners already may).
resource "google_compute_firewall" "operator_ssh" {
  name          = "${local.name_prefix}-ssh-through-iap"
  network       = google_compute_network.main.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["engine", "inference"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# The two machines reach the internet only to install packages and fetch the images and model weights,
# from one static address (an allow-list can name it). Cloud Run's own outbound traffic does not pass
# here: only its private ranges go into the network.
resource "google_compute_address" "egress" {
  name         = "${local.name_prefix}-egress"
  region       = var.region
  address_type = "EXTERNAL"

  depends_on = [google_project_service.main]
}

resource "google_compute_router" "egress" {
  name    = "${local.name_prefix}-router"
  region  = var.region
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "egress" {
  name                               = "${local.name_prefix}-nat"
  router                             = google_compute_router.egress.name
  region                             = var.region
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.egress.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  dynamic "subnetwork" {
    for_each = [google_compute_subnetwork.engine.id, google_compute_subnetwork.inference.id]
    content {
      name                    = subnetwork.value
      source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
    }
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
