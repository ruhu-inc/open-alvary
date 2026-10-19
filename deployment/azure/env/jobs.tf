# The public service's jobs, on its image, identity, secrets and settings:
# - migrate: launch checks, migrations and the seed, run by release.sh before each new revision;
# - retrieval: every minute, the engine catches up with the change log, embedding jobs run and the
#   section index is rebuilt when its analyzer changed (python -m retrieval.worker run);
# - worker and daily (activation B): monitors, digests and webhooks every five minutes; audit seals,
#   sweeps and purges once a day, as on Google Cloud.

locals {
  jobs = merge(
    {
      migrate = {
        command = ["sh", "-c", "python -m scripts.check_launch && alembic upgrade head && python -m scripts.seed"]
        cron    = null
        timeout = 900
        secrets = { POSTGRES_PASSWORD = "database-password" } # scripts.check_launch checks it
      }
      retrieval = {
        command = ["python", "-m", "retrieval.worker", "run"]
        cron    = "* * * * *"
        timeout = 55
        secrets = {}
      }
    },
    var.accounts_enabled ? {
      worker = {
        command = ["python", "-m", "changes.worker", "run"]
        cron    = "*/5 * * * *"
        timeout = 280
        secrets = {}
      }
      daily = {
        command = ["sh", "-c", "python -m accounts.seals seal && python -m changes.admin sweep && python -m changes.admin purge && python -m accounts.privacy purge && python -m accounts.seals verify"]
        cron    = "0 3 * * *"
        timeout = 600
        secrets = {}
      }
    } : {},
  )
}

resource "azurerm_container_app_job" "public" {
  for_each                     = local.public_enabled ? local.jobs : {}
  name                         = "${local.short_prefix}-${each.key}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = each.value.timeout
  replica_retry_limit          = 0
  tags                         = local.common_tags

  dynamic "manual_trigger_config" {
    for_each = each.value.cron == null ? [1] : []
    content {
      parallelism              = 1
      replica_completion_count = 1
    }
  }

  dynamic "schedule_trigger_config" {
    for_each = each.value.cron == null ? [] : [each.value.cron]
    content {
      cron_expression          = schedule_trigger_config.value
      parallelism              = 1
      replica_completion_count = 1
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.public.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.public.id
  }

  dynamic "secret" {
    for_each = toset(concat(local.secret_names, values(each.value.secrets)))
    content {
      name                = secret.value
      identity            = azurerm_user_assigned_identity.public.id
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${secret.value}"
    }
  }

  template {
    container {
      name    = each.key
      image   = var.public_image
      command = each.value.command
      cpu     = 0.5
      memory  = "1Gi"

      dynamic "env" {
        for_each = local.public_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = merge(local.public_secret_env, each.value.secrets)
        content {
          name        = env.key
          secret_name = env.value
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

  depends_on = [azurerm_role_assignment.pull, azurerm_role_assignment.secrets, azurerm_key_vault_secret.database_password]
}
