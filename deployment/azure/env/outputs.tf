output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "registry" {
  description = "Where release.sh builds images."
  value       = azurerm_container_registry.main.login_server
}

output "key_vault" {
  description = "Where secrets.sh copies the secrets."
  value       = azurerm_key_vault.main.name
}

output "storage" {
  value = {
    corpus  = azurerm_storage_account.corpus.name
    archive = azurerm_storage_account.archive.name
  }
}

output "database_host" {
  value = azurerm_postgresql_flexible_server.main.fqdn
}

output "engine_address" {
  value = local.engine_ip
}

output "inference_address" {
  value = local.inference_ip
}

output "front_door_endpoint" {
  description = "Check the service here before DNS moves; then CNAME both domains to it."
  value       = azurerm_cdn_frontdoor_endpoint.edge.host_name
}

output "dns_records" {
  description = "The records the owner adds: each domain's validation TXT first, then its CNAME at switchover."
  value = {
    for key, domain in azurerm_cdn_frontdoor_custom_domain.site : domain.host_name => {
      txt_name  = "_dnsauth.${domain.host_name}"
      txt_value = domain.validation_token
      cname     = azurerm_cdn_frontdoor_endpoint.edge.host_name
    }
  }
}

output "egress_address" {
  description = "The engine's and models' outbound address, for any allow-list."
  value       = azurerm_public_ip.egress.ip_address
}

output "ingestion_identity" {
  description = "Google's workload identity provider admits this principal only (README, Google from Azure)."
  value = {
    principal_id = azurerm_user_assigned_identity.ingestion.principal_id
    tenant_id    = data.azurerm_client_config.current.tenant_id
  }
}
