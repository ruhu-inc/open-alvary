# The ingestion jobs (phase 5), created when ingestion_image is set: reading scanned pages and
# preparing them for review, run by hand (deployment/azure/README.md). Reading stays on Google's
# Document AI for now (the owner's choice): the jobs reach it with a workload identity federation
# credential (google-wif-config), so no Google key is stored anywhere.

# transfer copies the buckets from Google Cloud inside Azure (phase 5): by default the corpus bucket;
# data.sh starts it with other arguments for the review collection and for verify.
resource "azurerm_container_app_job" "ingestion" {
  for_each = var.ingestion_image != "" ? {
    scan    = { args = ["-m", "ingestion.scan_job"], env = { SCAN_READER = "ocr" }, timeout = 86400 }
    prepare = { args = ["-m", "ingestion.prepare_job"], env = {}, timeout = 86400 }
    transfer = {
      args    = ["-m", "operations.transfer", "copy", "gs://open-alvary-corpus", "az://${azurerm_storage_account.corpus.name}/corpus"]
      env     = {}
      timeout = 86400
    }
  } : {}

  name                         = "${local.short_prefix}-${each.key}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = each.value.timeout
  replica_retry_limit          = 1
  tags                         = local.common_tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.ingestion.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.ingestion.id
  }

  secret {
    name                = "google-wif-config"
    identity            = azurerm_user_assigned_identity.ingestion.id
    key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/google-wif-config"
  }

  template {
    container {
      name    = each.key
      image   = var.ingestion_image
      command = ["python"]
      args    = each.value.args
      cpu     = 2
      memory  = "4Gi"

      dynamic "env" {
        for_each = merge(
          {
            ALVARY_CORPUS_BUCKET                      = "az://${azurerm_storage_account.corpus.name}/corpus"
            AZURE_CLIENT_ID                           = azurerm_user_assigned_identity.ingestion.client_id
            GOOGLE_APPLICATION_CREDENTIALS            = "/secrets/google-wif-config"
            GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES = "1" # the credential runs ingestion.google_federation
            ALVARY_GOOGLE_AUDIENCE                    = var.google_audience
          },
          var.document_ai,
          each.value.env,
        )
        content {
          name  = env.key
          value = env.value
        }
      }

      volume_mounts {
        name = "secrets"
        path = "/secrets"
      }
    }

    volume {
      name         = "secrets"
      storage_type = "Secret"
    }
  }

  depends_on = [azurerm_role_assignment.pull, azurerm_role_assignment.secrets]
}
