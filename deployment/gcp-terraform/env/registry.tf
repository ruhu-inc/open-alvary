# The images, built from a commit by deployment/gcp-terraform/release.sh (Cloud Build from git
# archive, as deployment/gcp/*.sh do today): nothing untracked or private enters an image.

resource "google_artifact_registry_repository" "main" {
  repository_id = local.project
  location      = var.region
  format        = "DOCKER"
  description   = "Open Alvary images, tagged by commit"

  depends_on = [google_project_service.main]
}

resource "google_artifact_registry_repository_iam_member" "builder" {
  repository = google_artifact_registry_repository.main.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.builder.member
}

# gcloud builds submit uploads the archive here; the build reads it as the builder account.
resource "google_storage_bucket" "build_sources" {
  name                        = "${var.project_id}_cloudbuild"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # only uploaded sources: nothing here is kept

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_storage_bucket_iam_member" "builder_sources" {
  bucket = google_storage_bucket.build_sources.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.builder.member
}

resource "google_project_iam_member" "builder_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = google_service_account.builder.member
}
