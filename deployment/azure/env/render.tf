# The page renderer (ingestion/render.py), created when render_image is set, as on Google Cloud
# (deployment/gcp-terraform/env/render.tf): pdftoppm draws the pages of outside PDFs for the workbench
# here, apart from everything the workbench holds. The app has no secret and no setting, and its ingress
# is internal: only apps in the environment reach it. Its one identity pulls its image and nothing more,
# and its lifecycle is None, so no container in the app can ask for a token with it.
#
# azurerm's container app has no place for an identity's lifecycle, so the app is written for the API
# itself, like the workbench's sign-in.

locals {
  render_enabled = var.render_image != ""
}

resource "azurerm_user_assigned_identity" "render_pull" {
  count               = local.render_enabled ? 1 : 0
  name                = "${local.name_prefix}-render-pull-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_role_assignment" "render_pull" {
  count                = local.render_enabled ? 1 : 0
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.render_pull[0].principal_id
}

resource "azapi_resource" "render" {
  count     = local.render_enabled ? 1 : 0
  type      = "Microsoft.App/containerApps@2025-01-01"
  name      = "${local.name_prefix}-render"
  parent_id = azurerm_resource_group.main.id
  location  = azurerm_resource_group.main.location
  tags      = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.render_pull[0].id]
  }

  body = {
    properties = {
      environmentId       = azurerm_container_app_environment.main.id
      workloadProfileName = "Consumption"
      configuration = {
        activeRevisionsMode = "Single"
        ingress = {
          external      = false
          targetPort    = 8080
          transport     = "auto" # HTTP/1.1 or HTTP/2 without TLS: Hypercorn speaks both
          allowInsecure = false
          traffic       = [{ latestRevision = true, weight = 100 }]
        }
        registries = [{
          server   = azurerm_container_registry.main.login_server
          identity = azurerm_user_assigned_identity.render_pull[0].id
        }]
        identitySettings = [{
          identity  = azurerm_user_assigned_identity.render_pull[0].id
          lifecycle = "None"
        }]
      }
      template = {
        containers = [{
          name      = "render"
          image     = var.render_image
          resources = { cpu = 1, memory = "2Gi" }
        }]
        scale = {
          minReplicas = 0
          maxReplicas = 4
          # One page at a time per replica: its memory is sized for one pdftoppm.
          rules = [{ name = "one-page-at-a-time", http = { metadata = { concurrentRequests = "1" } } }]
        }
      }
    }
  }

  response_export_values = ["properties.configuration.ingress.fqdn"]

  depends_on = [azurerm_role_assignment.render_pull]
}

locals {
  # What the workbench is told (workbench.tf): pages are drawn by the renderer, and nowhere else.
  render_env = local.render_enabled ? {
    ALVARY_RENDER     = "service"
    ALVARY_RENDER_URL = "https://${azapi_resource.render[0].output.properties.configuration.ingress.fqdn}"
  } : {}
}
