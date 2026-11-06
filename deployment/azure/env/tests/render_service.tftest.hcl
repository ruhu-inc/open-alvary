# The page renderer (render.tf), planned with stand-in providers: no cloud is touched and nothing needs
# signing in. `terraform init -backend=false && terraform test`. Off by default, nothing is made and the
# workbench draws pages as it did; on, the workbench draws them nowhere but the renderer, which has no
# secret, no setting, and no identity any container can use.

mock_provider "azurerm" {
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id = "00000000-0000-0000-0000-000000000001"
      object_id = "00000000-0000-0000-0000-000000000002"
    }
  }
}
mock_provider "azapi" {}
mock_provider "random" {}

override_resource {
  target          = azurerm_user_assigned_identity.render_pull
  override_during = plan
  values          = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/render-pull" }
}
override_resource {
  target          = azapi_resource.render
  override_during = plan
  values          = { output = { properties = { configuration = { ingress = { fqdn = "render.internal.example.azurecontainerapps.io" } } } } }
}

variables {
  subscription_id      = "00000000-0000-0000-0000-000000000000"
  environment          = "staging"
  operator_email       = "operator@example.org"
  public_domain        = "open.example.org"
  api_domain           = "api.open.example.org"
  operator_ip_ranges   = ["203.0.113.1"]
  workbench_image      = "registry.example.org/workbench:1"
  workbench_domain     = "review.example.org"
  public_image         = ""
  public_settings      = {}
  admin_ssh_public_key = "unused: the standby has no machines"
  engine_enabled       = false
  inference_enabled    = false
}

run "off_by_default" {
  command = plan

  assert {
    condition     = length(azapi_resource.render) == 0 && length(azurerm_user_assigned_identity.render_pull) == 0 && length(azurerm_role_assignment.render_pull) == 0
    error_message = "Nothing of the renderer is made until render_image is set."
  }
  assert {
    condition     = length([for name in keys(local.workbench_env) : name if startswith(name, "ALVARY_RENDER")]) == 0
    error_message = "The workbench is told nothing new: its image draws pages as before."
  }
}

run "on" {
  command = plan

  variables {
    render_image = "registry.example.org/render:1"
    # A setting that would draw pages in the workbench again is overruled.
    workbench_settings = { ALVARY_RENDER = "direct" }
  }

  assert {
    condition = (
      local.workbench_env["ALVARY_RENDER"] == "service"
      && local.workbench_env["ALVARY_RENDER_URL"] == "https://render.internal.example.azurecontainerapps.io"
      && !contains(keys(local.workbench_env), "ALVARY_RENDER_AUDIENCE")
    )
    error_message = "The workbench draws pages at the renderer, inside the environment, and nowhere else."
  }
  assert {
    condition     = azapi_resource.render[0].body.properties.configuration.ingress.external == false
    error_message = "The renderer takes requests from inside the environment only."
  }
  assert {
    condition = (
      !contains(keys(azapi_resource.render[0].body.properties.configuration), "secrets")
      && alltrue([for container in azapi_resource.render[0].body.properties.template.containers : !contains(keys(container), "env") && !contains(keys(container), "volumeMounts")])
    )
    error_message = "The renderer has no secret, no setting and no volume."
  }
  assert {
    condition = (
      length(azapi_resource.render[0].identity[0].identity_ids) == 1
      && one(azapi_resource.render[0].body.properties.configuration.identitySettings).lifecycle == "None"
      && one(azapi_resource.render[0].body.properties.configuration.registries).identity == one(azapi_resource.render[0].body.properties.configuration.identitySettings).identity
    )
    error_message = "Its one identity pulls its image, and no container can use it."
  }
  assert {
    condition     = azurerm_role_assignment.render_pull[0].role_definition_name == "AcrPull"
    error_message = "That identity may pull images and do nothing else."
  }
}
