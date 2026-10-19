# The application's secrets. Terraform makes the vault and the database URL; every other secret is
# copied from Google Cloud's Secret Manager by deployment/azure/secrets.sh, value for value, so API
# keys, feed tokens and webhook secrets made on Google Cloud keep working (the key pepper and the
# feed and webhook keys must never be regenerated). The apps read secrets by name, never by value.

resource "azurerm_key_vault" "main" {
  name                       = "${local.short_prefix}-kv"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 90
  tags                       = local.common_tags

  network_acls {
    default_action             = "Deny"
    bypass                     = "AzureServices"
    ip_rules                   = var.operator_ip_ranges
    virtual_network_subnet_ids = [azurerm_subnet.apps.id]
  }
}

# The operator running Terraform and secrets.sh manages secrets; the apps only read them.
resource "azurerm_role_assignment" "vault_operator" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_key_vault_secret" "database_url" {
  name         = "database-url"
  key_vault_id = azurerm_key_vault.main.id
  value = format(
    "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
    azurerm_postgresql_flexible_server.main.administrator_login,
    random_password.postgres.result,
    azurerm_postgresql_flexible_server.main.fqdn,
    local.database,
  )
  depends_on = [azurerm_role_assignment.vault_operator]
}

locals {
  # A secret's address for Container Apps: versionless, so a value copied again is read within
  # Container Apps' refresh (about 30 minutes) or at the next revision.
  secret = { for name in local.secret_names : name => "${azurerm_key_vault.main.vault_uri}secrets/${name}" }
  secret_names = concat(
    ["database-url", "feed-keys", "webhook-keys", "admission-config", "site-admission-config"],
    var.accounts_enabled ? ["key-pepper", "consent-secret", "console-client-secret", "resend-api-key"] : [],
    var.optional_secrets,
  )
}
