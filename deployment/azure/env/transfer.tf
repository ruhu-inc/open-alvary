# The database copy (phase 5): data.sh uploads a dump, the source's roles and its row counts to the
# transfer container; a restore job inside the network loads them into the new, empty server and
# compares the counts (deployment/azure/restore.py). Everything in the container is deleted after 14
# days. The restore jobs exist only while restore_image is set.

resource "azurerm_storage_container" "transfer" {
  name                  = "transfer"
  storage_account_id    = azurerm_storage_account.corpus.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "corpus" {
  storage_account_id = azurerm_storage_account.corpus.id

  rule {
    name    = "transfer-expires"
    enabled = true
    filters {
      prefix_match = ["transfer/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = 14
      }
      version {
        delete_after_days_since_creation = 14
      }
    }
  }
}

resource "azurerm_role_assignment" "transfer_read" {
  for_each             = { public = azurerm_user_assigned_identity.public.principal_id, workbench = azurerm_user_assigned_identity.workbench.principal_id }
  scope                = azurerm_storage_container.transfer.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = each.value
}

locals {
  restores = var.restore_image == "" ? {} : merge(
    {
      public = {
        identity = azurerm_user_assigned_identity.public
        secrets  = { DATABASE_URL = "database-url", ACCESS_ROLE_PASSWORD = "access-role-password" }
      }
    },
    local.workbench_enabled ? {
      review = {
        identity = azurerm_user_assigned_identity.workbench
        secrets  = { DATABASE_URL = "review-database-url" }
      }
    } : {},
  )
}

resource "azurerm_container_app_job" "restore" {
  for_each                     = local.restores
  name                         = "${local.short_prefix}-restore-${each.key}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 0
  tags                         = local.common_tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [each.value.identity.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = each.value.identity.id
  }

  dynamic "secret" {
    for_each = toset(values(each.value.secrets))
    content {
      name                = secret.value
      identity            = each.value.identity.id
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${secret.value}"
    }
  }

  template {
    container {
      name   = "restore"
      image  = var.restore_image
      cpu    = 1
      memory = "2Gi"

      env {
        name  = "AZURE_CLIENT_ID"
        value = each.value.identity.client_id
      }
      env {
        # data.sh uploads each database's files here, replacing the last ones.
        name  = "TRANSFER"
        value = "${azurerm_storage_account.corpus.primary_blob_endpoint}transfer/${each.key}/"
      }
      dynamic "env" {
        for_each = each.value.secrets
        content {
          name        = env.key
          secret_name = env.value
        }
      }
    }
  }

  depends_on = [azurerm_role_assignment.pull, azurerm_role_assignment.secrets, azurerm_role_assignment.transfer_read]
}
