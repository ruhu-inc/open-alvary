# Three buckets, as today's two on Google Cloud and Azure's two accounts:
# - corpus: staged originals, machine readings and the corpus collections (ingestion writes, the
#   workbench reads), versioned, in the EU multi-region, with Autoclass as today;
# - review collection: the workbench's first collection, mounted read-only into it as today;
# - archive: the write-once archive of fetched originals (CPR-05), whose retention policy makes every
#   object undeletable and unchangeable for archive_retention_days. Terraform creates the policy
#   unlocked; locking it is irreversible and is the owner's own step (README, "Locking the archive"),
#   taken only after the copy is checked. A bucket with a retention policy takes no versioning (Google
#   allows one or the other), and needs none: nothing in it can be replaced.
# None allows public access or per-object ACLs. Who reads and writes each is below.
#
# The corpus and the review collection can instead be today's buckets, used in place
# (existing_corpus_bucket, existing_review_bucket): nothing is copied, and Terraform only adds this
# environment's accounts to them, member by member. It never imports or changes those buckets, and
# removing a grant here removes only that member. No archive exists today, so it is always new.

resource "google_storage_bucket" "corpus" {
  count                       = var.existing_corpus_bucket == "" ? 1 : 0
  name                        = local.buckets.corpus
  location                    = "EU"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  autoclass {
    enabled = true # files nobody reads move to colder classes, with no retrieval fees
  }

  soft_delete_policy {
    retention_duration_seconds = 30 * 86400 # as Azure's 30-day delete retention
  }

  depends_on = [google_project_service.main]
}

resource "google_storage_bucket" "collection" {
  count                       = var.existing_review_bucket == "" ? 1 : 0
  name                        = local.buckets.collection
  location                    = "EU"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  depends_on = [google_project_service.main]
}

locals {
  corpus_bucket     = var.existing_corpus_bucket != "" ? var.existing_corpus_bucket : one(google_storage_bucket.corpus[*].name)
  collection_bucket = var.existing_review_bucket != "" ? var.existing_review_bucket : one(google_storage_bucket.collection[*].name)
}

resource "google_storage_bucket" "archive" {
  name                        = local.buckets.archive
  location                    = "EU" # multi-region: kept in more than one EU region, as Azure's GZRS
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  retention_policy {
    retention_period = tostring(var.archive_retention_days * 86400)
    is_locked        = false # the owner locks it (README); Terraform never does
  }

  depends_on = [google_project_service.main]
}

resource "google_storage_bucket_iam_member" "corpus_read" {
  bucket = local.corpus_bucket
  role   = "roles/storage.objectViewer"
  member = google_service_account.workbench.member
}

resource "google_storage_bucket_iam_member" "collection_read" {
  bucket = local.collection_bucket
  role   = "roles/storage.objectViewer"
  member = google_service_account.workbench.member
}

resource "google_storage_bucket_iam_member" "corpus_write" {
  bucket = local.corpus_bucket
  role   = "roles/storage.objectUser" # read, write and list, as the live jobs have it
  member = google_service_account.ingestion.member
}

# The copies (data.sh buckets) write the first collection into a bucket of the environment's own as
# the ingestion account; a collection used in place needs no copy and no writer.
resource "google_storage_bucket_iam_member" "collection_write" {
  count  = var.existing_review_bucket == "" ? 1 : 0
  bucket = local.collection_bucket
  role   = "roles/storage.objectUser"
  member = google_service_account.ingestion.member
}

# Create-only: Storage Object Creator adds objects and cannot delete or replace them, and the viewer
# role lets a copy see what is already there. Neither can remove anything, with or without the lock.
resource "google_storage_bucket_iam_member" "archive_write" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])
  bucket   = google_storage_bucket.archive.name
  role     = each.value
  member   = google_service_account.ingestion.member
}
