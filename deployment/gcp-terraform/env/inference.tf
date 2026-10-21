# The two models D29 selected, on one GPU (an L4 or a T4 holds both with room to spare): the embedder
# answers OpenAI-style /v1/embeddings on 8080 and the reranker TEI's /rerank on 8081, the APIs
# retrieval/vectors.py and retrieval/rerank.py speak. Each runs Hugging Face's text-embeddings-
# inference at the revision the development screen measured, so no query leaves Open Alvary's network.
# The project needs GPU quota for the chosen GPU in the region (README, "Before the first apply").
#
# The driver comes with the boot image (a Deep Learning VM image, NVIDIA's driver installed), in
# place of Azure's driver extension; cloud-init adds Docker and NVIDIA's container toolkit where the
# image lacks them. A GPU machine cannot move during Google's host maintenance, so it stops and
# starts again (on_host_maintenance TERMINATE); the services come back with it.

resource "google_compute_address" "inference" {
  count        = var.inference_enabled ? 1 : 0
  name         = "${local.name_prefix}-inference"
  region       = var.region
  subnetwork   = google_compute_subnetwork.inference.id
  address_type = "INTERNAL"
  address      = local.inference_ip
}

resource "google_compute_instance" "inference" {
  count                     = var.inference_enabled ? 1 : 0
  name                      = "${local.name_prefix}-inference"
  zone                      = var.inference_zone != "" ? var.inference_zone : var.zone
  machine_type              = local.inference_vm_size
  tags                      = ["inference"]
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = var.inference_boot_image
      size  = 128 # images and model weights
      type  = "pd-balanced"
    }
  }

  # The L4 comes with its machine type (g2); the T4 is attached to an n1 machine.
  dynamic "guest_accelerator" {
    for_each = local.gpu.accelerator == null ? [] : [local.gpu.accelerator]
    content {
      type  = guest_accelerator.value
      count = 1
    }
  }

  scheduling {
    on_host_maintenance = "TERMINATE"
    automatic_restart   = true
  }

  network_interface {
    subnetwork = google_compute_subnetwork.inference.id
    network_ip = google_compute_address.inference[0].address
  }

  metadata = {
    user-data = templatefile("${path.module}/inference-cloud-init.yaml.tftpl", {
      image    = local.tei_image
      ip       = local.inference_ip
      embedder = var.embedder
      reranker = var.reranker
    })
    install-nvidia-driver  = "True" # the Deep Learning VM images' own switch, if the driver is not yet in place
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
  }

  service_account {
    email  = google_service_account.machines.email
    scopes = ["cloud-platform"]
  }

  lifecycle {
    ignore_changes = [boot_disk[0].initialize_params[0].image, metadata["user-data"]]
  }

  depends_on = [google_compute_router_nat.egress]
}
