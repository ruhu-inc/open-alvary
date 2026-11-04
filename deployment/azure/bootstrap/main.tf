# Open Alvary's Terraform state: a container of its own in a state storage account, one file per environment.
# Run once, by the owner, before env/:
#
#   cd deployment/azure/bootstrap
#   terraform init && terraform apply -var subscription_id=<subscription id>
#
# State holds the database passwords and resource IDs, never the application's secrets: those are
# copied into Key Vault by deployment/azure/secrets.sh and only referenced by name.

terraform {
  required_version = ">= 1.9.0, < 2.0.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
}

provider "azurerm" {
  features {}
  subscription_id     = var.subscription_id
  storage_use_azuread = true
}

variable "subscription_id" {
  type = string
}

variable "state_resource_group" {
  type = string
}

variable "state_account" {
  type = string
}

data "azurerm_storage_account" "state" {
  name                = var.state_account
  resource_group_name = var.state_resource_group
}

resource "azurerm_storage_container" "state" {
  name                  = "open-alvary"
  storage_account_id    = data.azurerm_storage_account.state.id
  container_access_type = "private"
}

output "backend" {
  description = "Write these to deployment/azure/backend.hcl (see docs/deployment/azure-readme.md)."
  value = {
    resource_group_name  = var.state_resource_group
    storage_account_name = var.state_account
    container_name       = azurerm_storage_container.state.name
    use_azuread_auth     = true
  }
}
