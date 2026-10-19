output "project" {
  value = var.project_id
}

output "registry" {
  description = "Where release.sh pushes images."
  value       = local.registry
}

output "secrets" {
  description = "The secrets secrets.sh fills, by name; Terraform filled the generated ones."
  value       = sort(concat(keys(local.copied_secrets), keys(local.generated_secrets)))
}

output "storage" {
  value = {
    corpus     = local.corpus_bucket
    collection = local.collection_bucket
    archive    = google_storage_bucket.archive.name
    transfer   = google_storage_bucket.transfer.name
  }
}

output "database_host" {
  description = "The public database's private address."
  value       = google_sql_database_instance.main.private_ip_address
}

output "engine_address" {
  value = var.engine_enabled ? local.engine_ip : null
}

output "inference_address" {
  value = var.inference_enabled ? local.inference_ip : null
}

output "edge_address" {
  description = "The load balancer's address: check the service here before DNS moves (curl --resolve), then point both domains at it."
  value       = google_compute_global_address.edge.address
}

output "dns_records" {
  description = "The records the owner adds: each domain's certificate CNAME first, then its A record at switchover."
  value = {
    for key, domain in local.edge_domains : domain => {
      certificate_name  = google_certificate_manager_dns_authorization.edge[key].dns_resource_record[0].name
      certificate_type  = google_certificate_manager_dns_authorization.edge[key].dns_resource_record[0].type
      certificate_value = google_certificate_manager_dns_authorization.edge[key].dns_resource_record[0].data
      a                 = google_compute_global_address.edge.address
    }
  }
}

output "egress_address" {
  description = "The engine's and models' outbound address, for any allow-list."
  value       = google_compute_address.egress.address
}

output "workbench_url" {
  description = "The workbench, behind Identity-Aware Proxy (and its sign-in's OAuth redirect: this address plus /auth/callback)."
  value       = local.workbench_enabled ? "https://${local.workbench_host}" : null
}

output "ingestion_identity" {
  description = "The account the ingestion and copy jobs run as: the source bucket's owner grants it read (README, phase 5)."
  value       = google_service_account.ingestion.email
}

output "azure_credential_command" {
  description = "With azure_federation set: the credential configuration Azure's copy job uses, which names a command, not a key (README, Moving back from Azure)."
  value = var.azure_federation == null ? null : join(" ", [
    "gcloud iam workload-identity-pools create-cred-config",
    google_iam_workload_identity_pool_provider.azure[0].name,
    "--service-account=${google_service_account.ingestion.email}",
    "--executable-command='python -m ingestion.google_federation token'",
    "--executable-timeout-millis=30000 --output-file=google-wif-config.json",
  ])
}
