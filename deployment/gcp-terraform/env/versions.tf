terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
    # Identity-Aware Proxy's service agent (the workbench) is made only through the beta provider.
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State in the environment's own bucket, made by deployment/gcp-terraform/bootstrap:
  #   terraform init -backend-config=../staging.backend.hcl
  backend "gcs" {}
}
