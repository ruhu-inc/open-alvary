provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # Budgets (monitoring.tf) are a billing account's, reached with the operator's own credentials:
  # Google needs a project to bill those calls to, and this is it.
  user_project_override = true
  billing_project       = var.project_id

  default_labels = local.common_labels
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Read once the services are on: with user_project_override the read is billed to this project, and a
# fresh project has no Cloud Resource Manager API until main.tf switches it on.
data "google_project" "main" {
  project_id = var.project_id
  depends_on = [google_project_service.main]
}
