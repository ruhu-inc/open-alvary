# Open Alvary on Azure: one resource group per environment holds everything the environment runs
# (deployment/azure/README.md). Nothing here is shared with any other product: its own network,
# registry, vault, logs, database, storage and budget.

resource "azurerm_resource_group" "main" {
  name     = "${local.name_prefix}-rg"
  location = var.location
  tags     = local.common_tags
}
