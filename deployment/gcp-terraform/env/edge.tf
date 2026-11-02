# The one front door (D7, MAC-12), in place of Azure's Front Door: a global external HTTPS load
# balancer terminates TLS for the website's and the API's domains with Google-managed certificates
# and sends both to the public service through a serverless endpoint group, uncached (the service sets
# its own Cache-Control and ETags). Cloud Armor throttles each address on every path (D23), answering
# 429 above it: 600 a minute on /site/ and /downloads/, 1,200 on the pages and everything else, and
# 12,000 on the API's paths, above the largest plan's own rate. HTTP is redirected to HTTPS. The service
# takes traffic from the load balancer only (cloud_run.tf), as today.
#
# The certificates are Certificate Manager's, authorised by DNS (a CNAME per domain, dns_records in
# outputs.tf), so they are issued before the domains move: switchover is then an A record, with no
# window without a certificate. They and the address exist from phase 1; the rest needs the service.

resource "google_compute_global_address" "edge" {
  name       = "${local.name_prefix}-edge"
  ip_version = "IPV4"

  depends_on = [google_project_service.main]
}

locals {
  edge_domains = { site = var.public_domain, api = var.api_domain }
  # Anthropic's hosts: neither the edge rules nor the provider may block them from the MCP address or
  # its metadata (machine-access.md), and they carry many people's connections on few addresses.
  never_limited_range = "160.79.104.0/21"
  api_paths           = "request.path.startsWith('/api/') || request.path.startsWith('/mcp') || request.path.startsWith('/.well-known/')"
}

# Per-project records (_acme-challenge_<id>.<domain>), so a certificate for the same domain can be
# issued here while another project (today's open-alvary-prod) still holds and renews its own.
resource "google_certificate_manager_dns_authorization" "edge" {
  for_each = local.edge_domains
  name     = "${local.name_prefix}-${each.key}-dns"
  domain   = each.value
  type     = "PER_PROJECT_RECORD"

  depends_on = [google_project_service.main]
}

resource "google_certificate_manager_certificate" "edge" {
  for_each = local.edge_domains
  name     = "${local.name_prefix}-${each.key}"

  managed {
    domains            = [each.value]
    dns_authorizations = [google_certificate_manager_dns_authorization.edge[each.key].id]
  }
}

resource "google_certificate_manager_certificate_map" "edge" {
  name = "${local.name_prefix}-certs"

  depends_on = [google_project_service.main]
}

resource "google_certificate_manager_certificate_map_entry" "edge" {
  for_each     = local.edge_domains
  name         = "${local.name_prefix}-${each.key}"
  map          = google_certificate_manager_certificate_map.edge.name
  hostname     = each.value
  certificates = [google_certificate_manager_certificate.edge[each.key].id]
}

resource "google_compute_security_policy" "edge" {
  name        = "${local.name_prefix}-edge"
  description = "Open Alvary edge rules (D23)"

  # The first rule that matches decides, so each path is counted against one limit. Rule descriptions
  # may be at most 64 characters.
  rule {
    priority    = 900
    action      = "allow"
    description = "Anthropic's hosts on the MCP address and its metadata"

    match {
      expr {
        expression = "inIpRange(origin.ip, '${local.never_limited_range}') && (request.path.startsWith('/mcp') || request.path.startsWith('/.well-known/'))"
      }
    }
  }

  # A per-address throttle above any reader's pace; the origin's own site class does the fine-grained
  # admission. (Cloud Armor's expressions take no regular expression groups, so the paths are two
  # prefixes.)
  rule {
    priority    = 1000
    action      = "throttle"
    description = "The website's paths: ${var.edge_requests_per_minute} requests a minute per address"

    match {
      expr {
        expression = "request.path.startsWith('/site/') || request.path.startsWith('/downloads/')"
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.edge_requests_per_minute
        interval_sec = 60
      }
    }
  }

  # Never a challenge here: machine clients call these (D23). The ceiling stands above the largest
  # plan's rate, which the credential gate enforces per organisation; tests/test_edge_limits.py holds
  # it there.
  rule {
    priority    = 1100
    action      = "throttle"
    description = "API paths: ${var.edge_api_requests_per_minute} requests a minute per address"

    match {
      expr {
        expression = local.api_paths
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.edge_api_requests_per_minute
        interval_sec = 60
      }
    }
  }

  # Pages, the sitemap, the console, the health check: one script must not fill the service's few
  # instances and shut real readers out. High, because a mobile network can put many people on one
  # address.
  rule {
    priority    = 2000
    action      = "throttle"
    description = "Everything else: ${var.edge_page_requests_per_minute} requests a minute per address"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }

    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.edge_page_requests_per_minute
        interval_sec = 60
      }
    }
  }

  rule {
    priority    = 2147483647
    action      = "allow"
    description = "Everything else"

    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  depends_on = [google_project_service.main]
}

resource "google_compute_region_network_endpoint_group" "public" {
  count                 = local.public_enabled ? 1 : 0
  name                  = "${local.name_prefix}-public"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.public[0].name
  }
}

resource "google_compute_backend_service" "public" {
  count                 = local.public_enabled ? 1 : 0
  name                  = "${local.name_prefix}-public"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  security_policy       = google_compute_security_policy.edge.id

  backend {
    group = google_compute_region_network_endpoint_group.public[0].id
  }

  # The visitor's country, looked up here at the edge, for the website's daily totals (api.traffic,
  # TRF-02). The service keeps these two letters as a count, and only when counting is switched on.
  custom_request_headers = ["X-Client-Country:{client_region}"]

  # Requests are logged (Cloud Armor's decisions are in these logs); those for REST v1 and MCP, and
  # every one with a query string, are dropped before they are stored, so no query is ever kept
  # (monitoring.tf, OA-A2).
  log_config {
    enable      = true
    sample_rate = 1
  }
}

resource "google_compute_url_map" "public" {
  count           = local.public_enabled ? 1 : 0
  name            = "${local.name_prefix}-public"
  default_service = google_compute_backend_service.public[0].id
}

resource "google_compute_target_https_proxy" "public" {
  count           = local.public_enabled ? 1 : 0
  name            = "${local.name_prefix}-https"
  url_map         = google_compute_url_map.public[0].id
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.edge.id}"
}

resource "google_compute_global_forwarding_rule" "https" {
  count                 = local.public_enabled ? 1 : 0
  name                  = "${local.name_prefix}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.edge.id
  port_range            = "443"
  target                = google_compute_target_https_proxy.public[0].id
}

resource "google_compute_url_map" "redirect" {
  count = local.public_enabled ? 1 : 0
  name  = "${local.name_prefix}-redirect"

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  count   = local.public_enabled ? 1 : 0
  name    = "${local.name_prefix}-http"
  url_map = google_compute_url_map.redirect[0].id
}

resource "google_compute_global_forwarding_rule" "http" {
  count                 = local.public_enabled ? 1 : 0
  name                  = "${local.name_prefix}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_address            = google_compute_global_address.edge.id
  port_range            = "80"
  target                = google_compute_target_http_proxy.redirect[0].id
}
