terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # Container Apps authentication (the workbench's Entra sign-in) is not in azurerm 4.x.
    azapi = {
      source  = "azure/azapi"
      version = "~> 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State in the account deployment/azure/bootstrap made, one file per environment:
  #   terraform init -backend-config=../backend.hcl -backend-config="key=staging.tfstate"
  backend "azurerm" {}
}
