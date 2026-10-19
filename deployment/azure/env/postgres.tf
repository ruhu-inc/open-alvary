# PostgreSQL Flexible Server, VNet-integrated (no public address), in place of Cloud SQL. The same
# major version (16) as on Google Cloud, so the database moves by pg_dump and pg_restore
# (deployment/azure/data.sh). The administrator is open_alvary, the user the service connected as on
# Cloud SQL; the migrations create the restricted roles (rights/sql/provision) inside.

resource "random_password" "postgres" {
  length           = 32
  special          = true
  override_special = "_-" # safe in a URL
}

resource "azurerm_postgresql_flexible_server" "main" {
  name                          = "${local.name_prefix}-pg"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  version                       = var.postgres_version
  administrator_login           = "open_alvary"
  administrator_password        = random_password.postgres.result
  sku_name                      = var.postgres_sku_name
  storage_mb                    = var.postgres_storage_mb
  auto_grow_enabled             = true
  backup_retention_days         = var.postgres_backup_retention_days
  geo_redundant_backup_enabled  = var.environment == "prod"
  delegated_subnet_id           = azurerm_subnet.data.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  tags                          = local.common_tags

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]

  lifecycle {
    ignore_changes = [zone] # Azure picks a zone; never recreate the server over it
  }
}

resource "azurerm_postgresql_flexible_server_database" "main" {
  name      = local.database
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# The extensions the migrations create: pg_trgm (catalogue search) and pgvector (the dense arm's
# fallback store when the engine is absent). Azure refuses CREATE EXTENSION for any other.
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "PG_TRGM,VECTOR"
}
