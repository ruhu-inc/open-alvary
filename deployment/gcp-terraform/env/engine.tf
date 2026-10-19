# Vespa, one node to start (the owner's choice, RSV-04): its private address only, its index on a
# data disk, the application deployed and the units fed by the retrieval job (retrieval.projector),
# rebuilt from PostgreSQL whenever it is replaced. A second node and a content cluster per large
# jurisdiction come when the corpus needs them (retrieval/engine.py, CLUSTERS). With engine_enabled
# off there is no machine and the service is told of no engine (locals.tf).

resource "google_compute_address" "engine" {
  count        = var.engine_enabled ? 1 : 0
  name         = "${local.name_prefix}-engine"
  region       = var.region
  subnetwork   = google_compute_subnetwork.engine.id
  address_type = "INTERNAL"
  address      = local.engine_ip
}

resource "google_compute_disk" "engine" {
  count = var.engine_enabled ? 1 : 0
  name  = "${local.name_prefix}-engine-data"
  zone  = var.zone
  type  = "pd-ssd"
  size  = var.engine_disk_gb

  depends_on = [google_project_service.main]
}

resource "google_compute_instance" "engine" {
  count                     = var.engine_enabled ? 1 : 0
  name                      = "${local.name_prefix}-engine"
  zone                      = var.zone
  machine_type              = var.engine_vm_size
  tags                      = ["engine"]
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2204-lts"
      size  = 20
      type  = "pd-balanced"
    }
  }

  # The data disk appears as /dev/disk/by-id/google-vespa (engine-cloud-init.yaml.tftpl).
  attached_disk {
    source      = google_compute_disk.engine[0].id
    device_name = "vespa"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.engine.id
    network_ip = google_compute_address.engine[0].address
    # No access_config: no public address. Outbound goes through Cloud NAT (network.tf).
  }

  metadata = {
    user-data = templatefile("${path.module}/engine-cloud-init.yaml.tftpl", {
      image = var.vespa_image
      ip    = local.engine_ip
    })
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
  }

  service_account {
    email  = google_service_account.machines.email
    scopes = ["cloud-platform"] # what it may do is its account's roles: logs and metrics only
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  lifecycle {
    # A new image or start-up script never replaces the machine silently.
    ignore_changes = [boot_disk[0].initialize_params[0].image, metadata["user-data"]]
  }

  depends_on = [google_compute_router_nat.egress] # cloud-init installs packages on first boot
}
