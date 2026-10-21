# The one front door (D7, MAC-12), in place of the global load balancer and Cloud Armor: Front Door
# Standard terminates TLS for the website's and the API's domains with managed certificates and sends
# both to the public service, uncached (the service sets its own Cache-Control and ETags). The WAF
# policy throttles each address on the website's own paths (D23, as Cloud Armor did: 600 a minute on
# /site/ and /downloads/), answering 429 above it. The service refuses requests without this
# profile's ID (ALVARY_FRONT_DOOR_ID), so its own address is no way around the edge.
#
# The edge keeps no request log. Front Door's access and WAF logs record each request's full address,
# query string included, so they would keep what people search for (RSV-01, OA-A2). Azure collects them
# only through a diagnostic setting, and none exists: no azurerm_monitor_diagnostic_setting may send
# FrontDoorAccessLog or FrontDoorWebApplicationFirewallLog (or a category group, which includes them)
# anywhere, unless a future one first excludes or redacts query strings. Health probe logs and metrics
# carry no query. tests/test_request_logging.py fails on a setting that would send them.

resource "azurerm_cdn_frontdoor_profile" "edge" {
  name                     = "${local.name_prefix}-fd"
  resource_group_name      = azurerm_resource_group.main.name
  sku_name                 = "Standard_AzureFrontDoor"
  response_timeout_seconds = 60
  tags                     = local.common_tags
}

resource "azurerm_cdn_frontdoor_endpoint" "edge" {
  name                     = "${local.name_prefix}-fde"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.edge.id
  tags                     = local.common_tags
}

resource "azurerm_cdn_frontdoor_origin_group" "public" {
  count                    = local.public_enabled ? 1 : 0
  name                     = "public"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.edge.id
  session_affinity_enabled = false

  load_balancing {}

  health_probe {
    path                = "/healthz"
    protocol            = "Https"
    request_type        = "GET"
    interval_in_seconds = 60
  }
}

resource "azurerm_cdn_frontdoor_origin" "public" {
  count                          = local.public_enabled ? 1 : 0
  name                           = "public-service"
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.public[0].id
  enabled                        = true
  host_name                      = azurerm_container_app.public[0].ingress[0].fqdn
  origin_host_header             = azurerm_container_app.public[0].ingress[0].fqdn
  certificate_name_check_enabled = true
  http_port                      = 80
  https_port                     = 443
  priority                       = 1
  weight                         = 1000
}

resource "azurerm_cdn_frontdoor_custom_domain" "site" {
  for_each                 = { site = var.public_domain, api = var.api_domain }
  name                     = "${local.name_prefix}-${each.key}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.edge.id
  host_name                = each.value

  tls {
    certificate_type = "ManagedCertificate"
  }
}

resource "azurerm_cdn_frontdoor_route" "public" {
  count                           = local.public_enabled ? 1 : 0
  name                            = "public"
  cdn_frontdoor_endpoint_id       = azurerm_cdn_frontdoor_endpoint.edge.id
  cdn_frontdoor_origin_group_id   = azurerm_cdn_frontdoor_origin_group.public[0].id
  cdn_frontdoor_origin_ids        = [azurerm_cdn_frontdoor_origin.public[0].id]
  cdn_frontdoor_custom_domain_ids = [for domain in azurerm_cdn_frontdoor_custom_domain.site : domain.id]
  patterns_to_match               = ["/*"]
  supported_protocols             = ["Http", "Https"]
  https_redirect_enabled          = true
  forwarding_protocol             = "HttpsOnly"
  link_to_default_domain          = true # the endpoint's own name, for checks before DNS moves
}

resource "azurerm_cdn_frontdoor_firewall_policy" "edge" {
  name                              = replace("${local.name_prefix}-waf", "-", "")
  resource_group_name               = azurerm_resource_group.main.name
  sku_name                          = azurerm_cdn_frontdoor_profile.edge.sku_name
  enabled                           = true
  mode                              = "Prevention"
  custom_block_response_status_code = 429
  tags                              = local.common_tags

  custom_rule {
    name                           = "websitethrottle"
    type                           = "RateLimitRule"
    priority                       = 1000
    enabled                        = true
    action                         = "Block"
    rate_limit_duration_in_minutes = 1
    rate_limit_threshold           = var.edge_requests_per_minute

    match_condition {
      match_variable = "RequestUri"
      operator       = "Contains"
      match_values   = ["/site/", "/downloads/"]
    }
  }
}

resource "azurerm_cdn_frontdoor_security_policy" "edge" {
  name                     = "${local.name_prefix}-edge"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.edge.id

  security_policies {
    firewall {
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.edge.id
      association {
        patterns_to_match = ["/*"]
        domain {
          cdn_frontdoor_domain_id = azurerm_cdn_frontdoor_endpoint.edge.id
        }
        dynamic "domain" {
          for_each = azurerm_cdn_frontdoor_custom_domain.site
          content {
            cdn_frontdoor_domain_id = domain.value.id
          }
        }
      }
    }
  }
}
