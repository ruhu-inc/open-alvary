# Logs, the operator's alerts and the budget, as deployment/gcp/public_service.sh sets them up today.
# Request logs never carry a query or legal text: the service writes none (RSV-01), and the request
# logs Cloud Run and the load balancer write for REST v1 and MCP are dropped before they are stored
# (OA-A2). Cloud Logging keeps the rest 30 days (its default bucket), as Log Analytics does on Azure.

resource "google_monitoring_notification_channel" "operator" {
  display_name = "Open Alvary operator (${var.environment})"
  type         = "email"
  labels = {
    email_address = var.operator_email
  }

  depends_on = [google_project_service.main]
}

# No request logs for the API and MCP, so their queries are never kept: the app writes no access log,
# but Cloud Run and the load balancer each log every request with its full address, query included.
resource "google_logging_project_exclusion" "api_request_urls" {
  name        = "open-alvary-api-request-urls"
  description = "REST v1 and MCP request logs carry queries (OA-A2): never stored"
  filter = join(" ", [
    "(log_id(\"run.googleapis.com/requests\") OR resource.type=\"http_load_balancer\")",
    "AND (httpRequest.requestUrl:\"/api/v1\" OR httpRequest.requestUrl:\"/mcp\"",
    "OR httpRequest.requestUrl:\"//${var.api_domain}/\")",
  ])

  depends_on = [google_project_service.main]
}

# The audit seals' copy outside the database (MAC-13.7), kept 800 days. The bucket is created unlocked;
# once the first seals are in it, the owner locks its retention (irreversible):
#   gcloud logging buckets update audit-seals --location=<region> --locked --project=<project>
resource "google_logging_project_bucket_config" "audit_seals" {
  project        = var.project_id
  location       = var.region
  bucket_id      = "audit-seals"
  retention_days = 800
  description    = "Open Alvary audit log seals (MAC-13.7)"

  depends_on = [google_project_service.main]
}

resource "google_logging_project_sink" "audit_seals" {
  name                   = "${local.name_prefix}-audit-seals"
  destination            = "logging.googleapis.com/${google_logging_project_bucket_config.audit_seals.id}"
  filter                 = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${local.name_prefix}-daily\" AND jsonPayload.message=\"audit seal\""
  unique_writer_identity = true
}

# The public site answers: /healthz from Google's checkers every five minutes, once the domain points
# at this load balancer (domain_live); before that it could only fail.
resource "google_monitoring_uptime_check_config" "site" {
  count        = var.domain_live ? 1 : 0
  display_name = "Open Alvary website (/healthz, ${var.environment})"
  period       = "300s"
  timeout      = "10s"

  http_check {
    path         = "/healthz"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.public_domain
    }
  }

  depends_on = [google_project_service.main]
}

# Down from two or more of the checking regions for two checks running, so one region's network blip
# pages no one (as today).
resource "google_monitoring_alert_policy" "site_down" {
  count        = var.domain_live ? 1 : 0
  display_name = "Open Alvary: the website is down (${var.environment})"
  combiner     = "OR"

  conditions {
    display_name = "https://${var.public_domain}/healthz failing from two or more regions"
    condition_threshold {
      filter          = "metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id = \"${google_monitoring_uptime_check_config.site[0].uptime_check_id}\" AND resource.type = \"uptime_url\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "600s"
      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator.id]
  alert_strategy {
    auto_close = "86400s"
  }
  documentation {
    mime_type = "text/markdown"
    content   = "https://${var.public_domain}/healthz has failed for ten minutes from two or more regions. See deployment/runbook.md, Monitoring and alerts: check the service's revisions and logs, then the database."
  }
}

# A scheduled job failed (a broken audit seal fails the daily run), as today.
resource "google_monitoring_alert_policy" "job_failed" {
  count        = local.public_enabled && var.accounts_enabled ? 1 : 0
  display_name = "Open Alvary: a scheduled job failed (${var.environment})"
  combiner     = "OR"

  conditions {
    display_name = "A run of the daily or worker job failed"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\" AND (resource.labels.job_name = \"${local.name_prefix}-daily\" OR resource.labels.job_name = \"${local.name_prefix}-worker\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.job_name"]
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.operator.id]
  alert_strategy {
    auto_close = "86400s"
  }
  documentation {
    mime_type = "text/markdown"
    content   = "A scheduled job failed. For the daily job, look first for an ERROR line 'audit seal check' (deployment/runbook.md, Audit log seals); for the worker, see Monitors and webhooks."
  }
}

# The budget covers this project's spend before credits, so it shows what the startup credits are
# paying for (billing budgets work on credit-funded billing accounts; Azure's Sponsorship offer has no
# Cost Management, which is why its budget is off).
resource "google_billing_budget" "main" {
  count           = var.budget_enabled ? 1 : 0
  billing_account = var.billing_account
  display_name    = "${local.name_prefix} monthly"

  budget_filter {
    projects               = ["projects/${data.google_project.main.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      units = tostring(var.monthly_budget)
    }
  }

  dynamic "threshold_rules" {
    for_each = { actual-50 = [0.5, "CURRENT_SPEND"], actual-80 = [0.8, "CURRENT_SPEND"], actual-100 = [1.0, "CURRENT_SPEND"], forecast-100 = [1.0, "FORECASTED_SPEND"] }
    content {
      threshold_percent = threshold_rules.value[0]
      spend_basis       = threshold_rules.value[1]
    }
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.operator.id]
    disable_default_iam_recipients   = false
  }

  depends_on = [google_project_service.main]
}
