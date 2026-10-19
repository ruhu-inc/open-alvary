# One identity per workload, each with only what it needs: the public service (and its jobs) reads
# its secrets and pulls its image; the workbench reads its secrets and the corpus; ingestion writes
# the corpus and adds to the archive.

resource "azurerm_user_assigned_identity" "public" {
  name                = "${local.name_prefix}-public-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "workbench" {
  name                = "${local.name_prefix}-workbench-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "ingestion" {
  name                = "${local.name_prefix}-ingestion-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

locals {
  workloads = {
    public    = azurerm_user_assigned_identity.public.principal_id
    workbench = azurerm_user_assigned_identity.workbench.principal_id
    ingestion = azurerm_user_assigned_identity.ingestion.principal_id
  }
}

resource "azurerm_role_assignment" "pull" {
  for_each             = local.workloads
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = each.value
}

resource "azurerm_role_assignment" "secrets" {
  for_each             = local.workloads
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value
}

resource "azurerm_role_assignment" "corpus_read" {
  scope                = azurerm_storage_account.corpus.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.workbench.principal_id
}

resource "azurerm_role_assignment" "corpus_write" {
  scope                = azurerm_storage_account.corpus.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ingestion.principal_id
}

# Contributor on an immutable container can add blobs but cannot change or delete them while the
# retention policy holds them.
resource "azurerm_role_assignment" "archive_write" {
  scope                = azurerm_storage_account.archive.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ingestion.principal_id
}

# The operator copies the buckets from Google Cloud and checks them (deployment/azure/data.sh).
resource "azurerm_role_assignment" "operator_storage" {
  for_each             = { corpus = azurerm_storage_account.corpus.id, archive = azurerm_storage_account.archive.id }
  scope                = each.value
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}
