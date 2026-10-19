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

data "google_project" "main" {
  project_id = var.project_id
}
