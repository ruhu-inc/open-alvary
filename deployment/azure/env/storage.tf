# Two storage accounts, as the two buckets on Google Cloud:
# - corpus: staged originals and machine readings (ingestion writes, the workbench reads);
# - archive: the write-once archive of fetched originals (CPR-05), in a container whose time-based
#   retention policy makes every blob unchangeable for archive_retention_days. Terraform creates the
#   policy unlocked; locking it is irreversible and is the owner's own step (README, "Locking the
#   archive"), taken only after the copy from Google Cloud is checked.
# Neither allows shared keys or public access; both refuse every network but the apps subnet and the
# operator's addresses.

resource "azurerm_storage_account" "corpus" {
  name                            = local.storage_corpus
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  tags                            = local.common_tags

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
  }

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    ip_rules                   = [for range in var.operator_ip_ranges : trimsuffix(range, "/32")]
    virtual_network_subnet_ids = [azurerm_subnet.apps.id]
  }
}

resource "azurerm_storage_container" "corpus" {
  for_each              = toset(["corpus", "workbench"])
  name                  = each.key
  storage_account_id    = azurerm_storage_account.corpus.id
  container_access_type = "private"
}

resource "azurerm_storage_account" "archive" {
  name                            = local.storage_archive
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  account_tier                    = "Standard"
  account_replication_type        = var.environment == "prod" ? "GZRS" : "ZRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  tags                            = local.common_tags

  blob_properties {
    versioning_enabled = true
  }

  network_rules {
    default_action             = "Deny"
    bypass                     = ["AzureServices"]
    ip_rules                   = [for range in var.operator_ip_ranges : trimsuffix(range, "/32")]
    virtual_network_subnet_ids = [azurerm_subnet.apps.id]
  }
}

resource "azurerm_storage_container" "archive" {
  name                  = "originals"
  storage_account_id    = azurerm_storage_account.archive.id
  container_access_type = "private"
}

resource "azurerm_storage_container_immutability_policy" "archive" {
  storage_container_resource_manager_id = azurerm_storage_container.archive.id
  immutability_period_in_days           = var.archive_retention_days
  protected_append_writes_all_enabled   = false
  locked                                = false # the owner locks it (README); Terraform never does
}
