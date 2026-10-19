# The Container Apps environment, in the apps subnet, and the public service: one app serving the
# website, the docs, REST v1, MCP and the developer console (api.service), as on Cloud Run. It is
# reached only through Front Door (frontdoor.tf): requests without Front Door's ID are refused by the
# service (ALVARY_FRONT_DOOR_ID). Its jobs share its image, identity and settings (jobs.tf).

resource "azurerm_container_app_environment" "main" {
  name                       = "${local.name_prefix}-cae"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.apps.id
  tags                       = local.common_tags

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

locals {
  public_enabled = var.public_image != ""

  # Settings shared by the service and its jobs, as deployment/gcp/public_service.sh gave them.
  public_env = merge(
    {
      ALVARY_RUNTIME_MODE          = "persistent"
      PUBLIC_DOMAIN                = var.public_domain
      ALVARY_SITE_ORIGIN           = "https://${var.public_domain}"
      ALVARY_API_ORIGIN            = "https://${var.api_domain}"
      ALVARY_FEED_KEY_ID           = var.feed_key_id
      ALVARY_WEBHOOK_KEY_ID        = var.webhook_key_id
      ALVARY_ADMISSION_CONFIG      = "/secrets/admission-config"
      ALVARY_SITE_ADMISSION_CONFIG = "/secrets/site-admission-config"
      ALVARY_FRONT_DOOR_ID         = azurerm_cdn_frontdoor_profile.edge.resource_guid
    },
    var.public_settings,
    local.retrieval_env,
    var.accounts_enabled ? {
      ALVARY_ACCOUNTS          = "1"
      ALVARY_CONSOLE_ISSUER    = "https://${trimsuffix(trimprefix(var.auth0_domain, "https://"), "/")}/"
      ALVARY_CONSOLE_CLIENT_ID = var.console_client_id
      ALVARY_CONSOLE_BASE_URL  = "https://${var.public_domain}"
    } : {},
  )

  # Environment variable → Key Vault secret.
  public_secret_env = merge(
    {
      DATABASE_URL        = "database-url"
      ALVARY_FEED_KEYS    = "feed-keys"
      ALVARY_WEBHOOK_KEYS = "webhook-keys"
    },
    var.accounts_enabled ? {
      ALVARY_KEY_PEPPER            = "key-pepper"
      ALVARY_CONSENT_SECRET        = "consent-secret"
      ALVARY_CONSOLE_CLIENT_SECRET = "console-client-secret"
      RESEND_API_KEY               = "resend-api-key"
    } : {},
    { for name in var.optional_secrets : local.optional_secret_env[name] => name },
  )
  optional_secret_env = {
    "resend-webhook-secret"        = "RESEND_WEBHOOK_SECRET"
    "auth0-deletion-client-id"     = "ALVARY_AUTH0_DELETION_CLIENT_ID"
    "auth0-deletion-client-secret" = "ALVARY_AUTH0_DELETION_CLIENT_SECRET"
  }
}

resource "azurerm_container_app" "public" {
  count                        = local.public_enabled ? 1 : 0
  name                         = "${local.name_prefix}-public"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  tags                         = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.public.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.public.id
  }

  dynamic "secret" {
    for_each = toset(local.secret_names)
    content {
      name                = secret.value
      identity            = azurerm_user_assigned_identity.public.id
      key_vault_secret_id = local.secret[secret.value]
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
    min_replicas = var.public_min_replicas
    max_replicas = var.public_max_replicas

    container {
      name   = "public"
      image  = var.public_image
      cpu    = 1
      memory = "2Gi"

      dynamic "env" {
        for_each = local.public_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.public_secret_env
        content {
          name        = env.key
          secret_name = env.value
        }
      }

      volume_mounts {
        name = "secrets"
        path = "/secrets"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/healthz"
      }
    }

    # Each secret is a file named for it: the admission configurations are read from these.
    volume {
      name         = "secrets"
      storage_type = "Secret"
    }

    http_scale_rule {
      name                = "concurrency"
      concurrent_requests = "80"
    }
  }

  depends_on = [azurerm_role_assignment.pull, azurerm_role_assignment.secrets]
}
