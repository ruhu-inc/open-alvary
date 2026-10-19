# The review workbench (phase 4), created when workbench_image is set. As on Google Cloud it has a
# database server of its own, apart from the public service's, and reads the corpus account's
# workbench container (reconciled readings and page images). Entra ID admits operators before the
# workbench's own sign-in, in Identity-Aware Proxy's place (workbench_entra_client_id).

locals {
  workbench_enabled = var.workbench_image != ""

  workbench_env = merge(
    {
      ALVARY_REVIEW_BASE_URL      = "https://${var.workbench_domain}"
      ALVARY_REVIEW_ALLOWED_HOSTS = var.workbench_domain
    },
    var.workbench_settings,
  )
  workbench_secret_env = merge(
    {
      ALVARY_REVIEW_DATABASE_URL       = "review-database-url"
      ALVARY_REVIEW_OIDC_CLIENT_SECRET = "review-oidc-client-secret"
    },
    var.accounts_enabled ? {
      ALVARY_ACCESS_DATABASE_URL = "access-database-url"
      RESEND_API_KEY             = "resend-api-key"
    } : {},
    var.workbench_entra_client_id != "" ? { MICROSOFT_PROVIDER_AUTHENTICATION_SECRET = "workbench-entra-client-secret" } : {},
  )
}

resource "random_password" "review" {
  count            = local.workbench_enabled ? 1 : 0
  length           = 32
  special          = true
  override_special = "_-"
}

resource "azurerm_postgresql_flexible_server" "review" {
  count                         = local.workbench_enabled ? 1 : 0
  name                          = "${local.name_prefix}-review-pg"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  version                       = var.postgres_version
  administrator_login           = "workbench"
  administrator_password        = random_password.review[0].result
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  auto_grow_enabled             = true
  backup_retention_days         = 7
  delegated_subnet_id           = azurerm_subnet.data.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  tags                          = local.common_tags

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]

  lifecycle {
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "review" {
  count     = local.workbench_enabled ? 1 : 0
  name      = "workbench"
  server_id = azurerm_postgresql_flexible_server.review[0].id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_key_vault_secret" "review_database_url" {
  count        = local.workbench_enabled ? 1 : 0
  name         = "review-database-url"
  key_vault_id = azurerm_key_vault.main.id
  value = format(
    "postgresql+psycopg://workbench:%s@%s:5432/workbench?sslmode=require",
    random_password.review[0].result,
    azurerm_postgresql_flexible_server.review[0].fqdn,
  )
  depends_on = [azurerm_role_assignment.vault_operator]
}

resource "azurerm_container_app" "workbench" {
  count                        = local.workbench_enabled ? 1 : 0
  name                         = "${local.name_prefix}-workbench"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.workbench.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.workbench.id
  }

  dynamic "secret" {
    for_each = toset(values(local.workbench_secret_env))
    content {
      name                = secret.value
      identity            = azurerm_user_assigned_identity.workbench.id
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${secret.value}"
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8080
    transport        = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name    = "workbench"
      image   = var.workbench_image
      cpu     = 1
      memory  = "2Gi"
      command = ["uvicorn", "workbench.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]

      dynamic "env" {
        for_each = local.workbench_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = { for k, v in local.workbench_secret_env : k => v if k != "MICROSOFT_PROVIDER_AUTHENTICATION_SECRET" }
        content {
          name        = env.key
          secret_name = env.value
        }
      }
      env {
        name  = "AZURE_CLIENT_ID" # the corpus is read with this identity (ingestion/corpus_store.py)
        value = azurerm_user_assigned_identity.workbench.client_id
      }
    }
  }

  depends_on = [azurerm_role_assignment.pull, azurerm_role_assignment.secrets, azurerm_key_vault_secret.review_database_url]
}

resource "azurerm_container_app_custom_domain" "workbench" {
  count                    = local.workbench_enabled && var.workbench_domain != "" ? 1 : 0
  name                     = var.workbench_domain
  container_app_id         = azurerm_container_app.workbench[0].id
  certificate_binding_type = "SniEnabled"

  lifecycle {
    ignore_changes = [certificate_binding_type, container_app_environment_certificate_id] # the managed certificate is bound after DNS validates
  }
}

# Entra ID in front of the workbench: only signed-in members of the tenant reach its own sign-in.
resource "azapi_resource" "workbench_auth" {
  count     = local.workbench_enabled && var.workbench_entra_client_id != "" ? 1 : 0
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.workbench[0].id

  body = {
    properties = {
      platform = { enabled = true }
      globalValidation = {
        unauthenticatedClientAction = "RedirectToLoginPage"
        redirectToProvider          = "azureactivedirectory"
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId                = var.workbench_entra_client_id
            clientSecretSettingName = "workbench-entra-client-secret"
            openIdIssuer            = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
          }
        }
      }
    }
  }
}
