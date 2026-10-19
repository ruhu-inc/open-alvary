# Open Alvary's Terraform state on Google Cloud: a bucket in each environment's own project, versioned,
# so deleting a project takes its state with it and nothing else. Run once, by the owner, after the
# projects exist and before env/:
#
#   cd deployment/gcp-terraform/bootstrap
#   terraform init && terraform apply -var 'projects={staging="open-alvary-staging",prod="<fresh prod project>"}'
#
# This module's own state is small and stays on the operator's machine (ignored by git). State holds
# the database passwords and URLs and resource IDs, never the application's other secrets: those are
# copied into Secret Manager by deployment/gcp-terraform/secrets.sh and only referenced by name.

terraform {
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.0" }
  }
}

provider "google" {}

variable "projects" {
  description = "Each environment's project (staging, prod), made fresh for it."
  type        = map(string)
  validation {
    condition     = alltrue([for env, project in var.projects : contains(["staging", "prod"], env) && !contains(["open-alvary-prod", "open-alvary-ingestion"], project)])
    error_message = "Name staging and prod, each with a fresh project: never a live project built by deployment/gcp/*.sh."
  }
}

variable "location" {
  description = "Where the state is kept (EU, as the data)."
  type        = string
  default     = "EU"
}

resource "google_storage_bucket" "state" {
  for_each                    = var.projects
  name                        = "${each.value}-tfstate"
  project                     = each.value
  location                    = var.location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true # every state Terraform wrote can be brought back
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 20
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    project     = "open-alvary"
    environment = each.key
    managed-by  = "terraform"
  }

  lifecycle {
    prevent_destroy = true
  }
}

output "backend" {
  description = "Write each to deployment/gcp-terraform/<env>.backend.hcl (see deployment/gcp-terraform/README.md)."
  value = {
    for env, bucket in google_storage_bucket.state : env => {
      bucket = bucket.name
      prefix = "open-alvary"
    }
  }
}
