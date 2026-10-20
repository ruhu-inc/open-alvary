# The public service's jobs, on its image, identity, secrets and settings. A job without a cron is a
# manual one, which runs only when started (release.sh, or az containerapp job start):
# - migrate: launch checks, migrations and the seed, run by release.sh before each new revision;
# - retrieval: every minute, the engine catches up with the change log, embedding jobs run and the
#   section index is rebuilt when its analyzer changed (python -m retrieval.worker run);
# - worker and daily (activation B): monitors, digests and webhooks every five minutes; audit seals,
#   sweeps and purges once a day at 03:15 UTC, as on Google Cloud. The daily job also writes each
#   audit seal to the archive account's audit-seals container (storage.tf), as its own identity.
#   Until the public domains point here (domain_live), the two are manual jobs (Google Cloud pauses
#   their schedules instead): only the live environment writes to the outside world. A Container
#   Apps job's schedule cannot be paused, and azurerm cannot change a job's trigger in place, so
#   changing domain_live replaces both jobs; all they keep is in the database.

locals {
  jobs = merge(
    {
      migrate = {
        command = ["sh", "-c", "python -m scripts.check_launch && alembic upgrade head && python -m scripts.seed"]
        cron    = null
        timeout = 900
        secrets = { POSTGRES_PASSWORD = "database-password" } # scripts.check_launch checks it
        env     = {}
      }
      retrieval = {
        command = ["python", "-m", "retrieval.worker", "run"]
        cron    = "* * * * *"
        timeout = 55
        secrets = {}
        env     = {}
      }
    },
    var.accounts_enabled ? {
      worker = {
        command = ["python", "-m", "changes.worker", "run"]
        cron    = var.domain_live ? "*/5 * * * *" : null
        timeout = 280
        secrets = {}
        env     = {}
      }
      daily = {
        command = ["sh", "-c", "python -m accounts.seals seal && python -m changes.admin sweep && python -m changes.admin purge && python -m accounts.privacy purge && python -m accounts.seals verify"]
        cron    = var.domain_live ? "15 3 * * *" : null
        timeout = 600
        secrets = {}
        env = {
          ALVARY_AUDIT_SEALS = "az://${azurerm_storage_account.archive.name}/${azurerm_storage_container.audit_seals.name}"
          AZURE_CLIENT_ID    = azurerm_user_assigned_identity.public.client_id
        }
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
        for_each = merge(local.public_env, each.value.env)
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

  depends_on = [
    azurerm_role_assignment.pull,
    azurerm_role_assignment.secrets,
    azurerm_role_assignment.audit_seals_write,
    azurerm_key_vault_secret.database_password,
  ]
}
