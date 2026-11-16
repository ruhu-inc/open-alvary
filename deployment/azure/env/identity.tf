# One identity per workload, each with only what it needs: the public service (and its jobs) reads
# its secrets, pulls its image and adds the audit seals; the workbench reads its secrets and the
# corpus; ingestion writes the corpus and adds to the archive.

resource "azurerm_user_assigned_identity" "public" {
  name                = "${local.name_prefix}-public-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "workbench" {
  name                = "${local.name_prefix}-workbench-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "ingestion" {
  name                = "${local.name_prefix}-ingestion-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

locals {
  workloads = {
    public    = azurerm_user_assigned_identity.public.principal_id
    workbench = azurerm_user_assigned_identity.workbench.principal_id
    ingestion = azurerm_user_assigned_identity.ingestion.principal_id
  }
}

resource "azurerm_role_assignment" "pull" {
  for_each             = local.workloads
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = each.value
}

# Each identity reads the secrets it uses and no other (findings 2026-11-16, item 41: every workload could
# read the whole vault). A grant is on the secret itself, so a secret added later is read by nobody until
# it is named here.
locals {
  secret_reads = {
    public = toset(concat(
      local.secret_names,
      values(local.service_database_env),
      flatten([for name, job in local.jobs : concat(values(job.secrets), [local.job_database[name]])]),
      try(values(local.restores.public.secrets), []),
    ))
    workbench = toset(concat(values(local.workbench_secret_env), try(values(local.restores.review.secrets), [])))
    ingestion = toset(["google-wif-config"])
  }
  secret_grants = {
    for pair in flatten([
      for workload, names in local.secret_reads : [for name in names : { workload = workload, name = name }]
    ]) : "${pair.workload}/${pair.name}" => pair
  }
}

resource "azurerm_role_assignment" "secrets" {
  for_each             = local.secret_grants
  scope                = "${azurerm_key_vault.main.id}/secrets/${each.value.name}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = local.workloads[each.value.workload]
}

resource "azurerm_role_assignment" "corpus_read" {
  scope                = azurerm_storage_account.corpus.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.workbench.principal_id
}

resource "azurerm_role_assignment" "corpus_write" {
  scope                = azurerm_storage_account.corpus.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ingestion.principal_id
}

# Contributor on an immutable container can add blobs but cannot change or delete them while the
# retention policy holds them.
resource "azurerm_role_assignment" "archive_write" {
  scope                = azurerm_storage_account.archive.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ingestion.principal_id
}

# The daily job adds each audit seal (accounts/seals.py), on that container alone.
resource "azurerm_role_assignment" "audit_seals_write" {
  scope                = azurerm_storage_container.audit_seals.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.public.principal_id
}

# The operator copies the buckets from Google Cloud and checks them (deployment/azure/data.sh).
resource "azurerm_role_assignment" "operator_storage" {
  for_each             = { corpus = azurerm_storage_account.corpus.id, archive = azurerm_storage_account.archive.id }
  scope                = each.value
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}
