# Open Alvary on Google Cloud: one project per environment holds everything the environment runs
# (deployment/gcp-terraform/README.md), as a resource group does on Azure. Nothing here is shared with
# any other product: its own network, registry, secrets, logs, databases, buckets and budget.
#
# The project is made by the owner before the first apply (README, "Before the first apply"), so
# this file only switches on the services the environment uses. Every resource that needs one of
# them waits for all of them (depends_on), so a first apply never races an API being enabled.

locals {
  services = concat(
    [
      "artifactregistry.googleapis.com",
      "certificatemanager.googleapis.com",
      "cloudbuild.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "cloudscheduler.googleapis.com",
      "compute.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "iap.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "servicenetworking.googleapis.com",
      "serviceusage.googleapis.com",
      "sqladmin.googleapis.com",
      "storage.googleapis.com",
      "sts.googleapis.com",
    ],
    var.ingestion_image != "" ? ["documentai.googleapis.com"] : [],
    var.budget_enabled ? ["billingbudgets.googleapis.com"] : [],
  )
}

resource "google_project_service" "main" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false # turning an API off could break what another apply still holds
}
