# Logs, the operator's alerts and the budget. Request logs never carry a query or legal text: the
# service writes none (RSV-01), and Container Apps' console logs hold only what it writes.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${local.name_prefix}-logs"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 2 # a runaway log loop costs at most this much a day
  tags                = local.common_tags
}

resource "azurerm_application_insights" "main" {
  name                = "${local.name_prefix}-appi"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = local.common_tags
}

resource "azurerm_monitor_action_group" "operator" {
  name                = "${local.name_prefix}-operator"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "oa-${local.storage_env}"
  tags                = local.common_tags

  email_receiver {
    name                    = "operator"
    email_address           = var.operator_email
    use_common_alert_schema = true
  }
}

# The public site answers: an availability test from three regions every five minutes.
resource "azurerm_application_insights_standard_web_test" "site" {
  name                    = "${local.name_prefix}-site"
  location                = azurerm_resource_group.main.location
  resource_group_name     = azurerm_resource_group.main.name
  application_insights_id = azurerm_application_insights.main.id
  geo_locations           = ["emea-se-sto-edge", "emea-nl-ams-azr", "emea-gb-db3-azr"]
  frequency               = 300
  enabled                 = true
  tags                    = local.common_tags

  request {
    url = "https://${var.public_domain}/healthz"
  }
  validation_rules {
    expected_status_code = 200
  }
}

resource "azurerm_monitor_metric_alert" "site_down" {
  name                = "${local.name_prefix}-site-down"
  resource_group_name = azurerm_resource_group.main.name
  scopes              = [azurerm_application_insights_standard_web_test.site.id, azurerm_application_insights.main.id]
  description         = "The public site failed its availability test from two or more locations."
  severity            = 1
  tags                = local.common_tags

  application_insights_web_test_location_availability_criteria {
    web_test_id           = azurerm_application_insights_standard_web_test.site.id
    component_id          = azurerm_application_insights.main.id
    failed_location_count = 2
  }

  action {
    action_group_id = azurerm_monitor_action_group.operator.id
  }
}

# The budget covers everything in the environment's resource group. Cost Management, which budgets
# need, does not report on Sponsorship subscriptions (ruhu-sub is one): usage and the credit left are
# in Microsoft's sponsorship portal instead, so the budget is off unless the subscription supports it.
resource "azurerm_consumption_budget_resource_group" "main" {
  count             = var.budget_enabled ? 1 : 0
  name              = "${local.name_prefix}-budget"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.monthly_budget
  time_grain        = "Monthly"

  time_period {
    start_date = "2026-11-01T00:00:00Z"
  }

  dynamic "notification" {
    for_each = { actual-50 = [50, "Actual"], actual-80 = [80, "Actual"], actual-100 = [100, "Actual"], forecast-100 = [100, "Forecasted"] }
    content {
      enabled        = true
      threshold      = notification.value[0]
      threshold_type = notification.value[1]
      operator       = "GreaterThanOrEqualTo"
      contact_emails = [var.operator_email]
      contact_groups = [azurerm_monitor_action_group.operator.id]
    }
  }
}
