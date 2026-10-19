# One service account per workload, each with only what it needs: the public service (and its jobs)
# reads its own secrets and connects to Cloud SQL; the workbench reads its secrets, the corpus and its
# collection; ingestion writes the corpus, adds to the archive and calls Document AI, natively (no
# federation on Google Cloud). Images are pulled by Cloud Run's own service agent from this
# project's registry, so no workload needs a pull role (Azure's AcrPull). Grants on a single secret
# or bucket are beside it (secrets.tf, storage.tf, transfer.tf).

resource "google_service_account" "public" {
  account_id   = "${local.name_prefix}-public"
  display_name = "Open Alvary public service and its jobs (${var.environment})"

  depends_on = [google_project_service.main]
}

resource "google_service_account" "workbench" {
  account_id   = "${local.name_prefix}-workbench"
  display_name = "Open Alvary review workbench (${var.environment})"

  depends_on = [google_project_service.main]
}

resource "google_service_account" "ingestion" {
  account_id   = "${local.name_prefix}-ingestion"
  display_name = "Open Alvary ingestion and copy jobs (${var.environment})"

  depends_on = [google_project_service.main]
}

# Cloud Scheduler calls the scheduled jobs as this account, which may only start them (jobs.tf).
resource "google_service_account" "scheduler" {
  account_id   = "${local.name_prefix}-scheduler"
  display_name = "Starts the Open Alvary scheduled jobs (${var.environment})"

  depends_on = [google_project_service.main]
}

# Cloud Build runs release.sh's builds as this account: it reads the uploaded sources, pushes to the
# registry and writes the build's logs, nothing else (registry.tf).
resource "google_service_account" "builder" {
  account_id   = "${local.name_prefix}-builder"
  display_name = "Open Alvary image builds (${var.environment})"

  depends_on = [google_project_service.main]
}

# The engine and the GPU machine run as this account: their logs and metrics, nothing else.
resource "google_service_account" "machines" {
  account_id   = "${local.name_prefix}-vm"
  display_name = "Open Alvary engine and GPU machines (${var.environment})"

  depends_on = [google_project_service.main]
}

# The Cloud SQL client role is kept for parity with the live system (and lets an operator use the
# Cloud SQL Auth Proxy as these accounts); the service itself connects over the private network.
resource "google_project_iam_member" "sql_client" {
  for_each = { public = google_service_account.public.member, workbench = google_service_account.workbench.member }
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = each.value
}

# Document AI's user role, in the project that holds the processors (ALVARY_DOCAI_PROJECT: today
# open-alvary-ingestion, whose processors the readings and the routing calibration were measured on),
# else this one. In another project Terraform adds only this member, never managing that project.
resource "google_project_iam_member" "document_ai" {
  count   = var.ingestion_image != "" ? 1 : 0
  project = lookup(var.document_ai, "ALVARY_DOCAI_PROJECT", var.project_id)
  role    = "roles/documentai.apiUser"
  member  = google_service_account.ingestion.member
}

resource "google_project_iam_member" "machines" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.value
  member   = google_service_account.machines.member
}

# Moving back from Azure (README): Google's token service exchanges the Azure ingestion identity's
# Entra token for this project's ingestion account, so Azure's copy job writes the buckets with no
# key, the reverse of deployment/azure/README.md's "Google from Azure". Only that one principal is
# admitted; the whole is removed by setting azure_federation back to null.
resource "google_iam_workload_identity_pool" "azure" {
  count                     = var.azure_federation == null ? 0 : 1
  workload_identity_pool_id = "azure"
  display_name              = "Azure (Open Alvary)"

  depends_on = [google_project_service.main]
}

resource "google_iam_workload_identity_pool_provider" "azure" {
  count                              = var.azure_federation == null ? 0 : 1
  workload_identity_pool_id          = google_iam_workload_identity_pool.azure[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "azure"
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition                = "assertion.sub == '${var.azure_federation.principal_id}'"

  oidc {
    issuer_uri        = "https://sts.windows.net/${var.azure_federation.tenant_id}/"
    allowed_audiences = [var.azure_federation.audience]
  }
}

resource "google_service_account_iam_member" "azure_federation" {
  count              = var.azure_federation == null ? 0 : 1
  service_account_id = google_service_account.ingestion.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.azure[0].name}/subject/${var.azure_federation.principal_id}"
}
