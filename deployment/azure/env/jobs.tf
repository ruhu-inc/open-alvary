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
# - logins: makes or repairs the restricted database logins (rights/logins.py), started by hand. A job
#   named in database_logins_in_use signs in with its own login.

locals {
  jobs = merge(
    {
      migrate = {
        command = ["sh", "-c", "python -m scripts.check_launch && alembic upgrade head && python -m scripts.seed${var.database_logins_ready ? " && python -m rights.logins grants" : ""}"]
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
      # The semantic index's operator commands, run by hand: start it with the command of each run
      # (az containerapp job start --command / --args; README of deployment/gcp-terraform, "Semantic index").
      indexer = {
        command = ["python", "-m", "retrieval.indexer", "status"]
        cron    = null
        timeout = 900
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
        command = ["sh", "-c", "python -m accounts.seals seal && python -m changes.admin sweep && python -m changes.admin purge && python -m accounts.privacy purge && python -m accounts.seals verify && python -m api.traffic maintain"]
        cron    = var.domain_live ? "15 3 * * *" : null
        timeout = 600
        secrets = {}
        env = {
          ALVARY_AUDIT_SEALS = "az://${azurerm_storage_account.archive.name}/${azurerm_storage_container.audit_seals.name}"
          AZURE_CLIENT_ID    = azurerm_user_assigned_identity.public.client_id
        }
      }
      # New terms, emailed to organisations' owners (runbook, "New terms"), started by hand with
      # --args: the operator, then the notice's arguments.
      terms = {
        command = ["sh", "-c", "exec python -m accounts.operator --operator \"$0\" terms-notice \"$@\""]
        cron    = null
        timeout = 900
        secrets = {}
        env     = {}
      }
      # The workbench's database role and its grants (runbook, "Website traffic counts"), started by hand
      # after a migration that adds something the Access or Traffic section reads. Grants only.
      access-role = {
        command = ["python", "-m", "accounts.access_admin", "provision-role", "open_alvary_access"]
        cron    = null
        timeout = 300
        secrets = {}
        env     = {}
      }
    } : {},
    # Makes or repairs the restricted logins, safe to run again; with the argument check, signs in as each
    # and proves what it can and cannot do, changing nothing (the runbook's "Database logins").
    var.database_logins_ready ? {
      logins = {
        command = ["python", "-m", "rights.logins", "provision"]
        cron    = null
        timeout = 300
        secrets = { for login in keys(local.database_logins) : "ALVARY_LOGIN_${upper(login)}_URL" => "database-url-${login}" }
        env     = {}
      }
    } : {},
  )
  job_database = {
    for name in keys(local.jobs) : name => lookup(local.database_secret, name, "database-url")
  }
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
    for_each = toset(concat(local.secret_names, values(each.value.secrets), [local.job_database[each.key]]))
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
        for_each = merge(local.public_secret_env, each.value.secrets, { DATABASE_URL = local.job_database[each.key] })
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
    azurerm_key_vault_secret.database_login,
  ]
}
