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

# The restricted logins (rights/logins.py), each with what its workloads do and no more; the logins job
# makes them from these URLs. Each password is generated here and reaches the job only through the vault.
resource "random_password" "database_login" {
  for_each = local.database_logins
  length   = 40
  special  = false
}

resource "azurerm_key_vault_secret" "database_login" {
  for_each     = local.database_logins
  name         = "database-url-${each.key}"
  key_vault_id = azurerm_key_vault.main.id
  value = format(
    "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
    each.value,
    random_password.database_login[each.key].result,
    azurerm_postgresql_flexible_server.main.fqdn,
    local.database,
  )
  depends_on = [azurerm_role_assignment.vault_operator]
}

# The migrate job's launch check reads the password itself (scripts.check_launch, POSTGRES_PASSWORD),
# as on Google Cloud.
resource "azurerm_key_vault_secret" "database_password" {
  name         = "database-password"
  key_vault_id = azurerm_key_vault.main.id
  value        = random_password.postgres.result
  depends_on   = [azurerm_role_assignment.vault_operator]
}

locals {
  # A secret's address for Container Apps: versionless, so a value copied again is read within
  # Container Apps' refresh (about 30 minutes) or at the next revision.
  secret = {
    for name in concat(local.secret_names, values(local.service_database_env)) :
    name => "${azurerm_key_vault.main.vault_uri}secrets/${name}"
  }
  database_logins = var.database_logins_ready ? {
    service   = "open_alvary_service"
    catalogue = "open_alvary_catalogue"
    jobs      = "open_alvary_jobs"
    retrieval = "open_alvary_retrieval"
  } : {}
  # The catalogue is the public service's routes that serve it (api.service): until it is named, the
  # service serves them on its own login.
  workload_login = {
    service   = "service"
    catalogue = "catalogue"
    worker    = "jobs"
    daily     = "jobs"
    terms     = "jobs"
    retrieval = "retrieval"
    indexer   = "retrieval"
  }
  # Workload → the secret holding the URL it signs in with.
  database_secret = {
    for workload, login in local.workload_login :
    workload => contains(var.database_logins_in_use, workload) ? "database-url-${login}" : "database-url"
  }
  # The service's own database URLs: its login, and once database_logins_in_use names the catalogue, a
  # login of the catalogue's own for every route that serves it, which reaches no account.
  service_database_env = merge(
    { DATABASE_URL = local.database_secret["service"] },
    contains(var.database_logins_in_use, "catalogue") ? { ALVARY_CATALOGUE_DATABASE_URL = local.database_secret["catalogue"] } : {},
  )
  # Every workload's but its database URL, which is its own.
  secret_names = concat(
    ["feed-keys", "webhook-keys", "admission-config", "site-admission-config"],
    var.accounts_enabled ? ["key-pepper", "consent-secret", "console-client-secret", "resend-api-key"] : [],
    var.optional_secrets,
  )
}
