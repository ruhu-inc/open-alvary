# The images, built from a commit by deployment/azure/release.sh (az acr build from git archive, as
# Cloud Build did): nothing untracked or private enters an image.

resource "azurerm_container_registry" "main" {
  name                = local.registry_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.common_tags
}
