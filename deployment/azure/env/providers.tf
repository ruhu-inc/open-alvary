provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false # purge protection is on: a deleted vault waits out its retention
    }
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
  subscription_id     = var.subscription_id
  storage_use_azuread = true # storage accounts allow no shared keys
}

provider "azapi" {
  subscription_id = var.subscription_id
}

data "azurerm_client_config" "current" {}
