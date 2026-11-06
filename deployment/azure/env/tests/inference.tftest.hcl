# The inference machine with a GPU and without one, planned with stand-in providers: no cloud is touched
# and nothing needs signing in. `terraform init -backend=false && terraform test`. By default it is the
# T4 machine it was; inference_gpu = none puts the same two models on CPUs, at the same address.

mock_provider "azurerm" {
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      object_id       = "00000000-0000-0000-0000-000000000002"
      subscription_id = "00000000-0000-0000-0000-000000000003"
    }
  }
}
mock_provider "azapi" {}
mock_provider "random" {}

variables {
  subscription_id      = "00000000-0000-0000-0000-000000000003"
  environment          = "staging"
  operator_email       = "operator@example.org"
  public_domain        = "open.example.org"
  api_domain           = "api.open.example.org"
  public_settings      = {}
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJs+K4RWnHYUuN5w2xCuNxO+1CG5YPNBBhjJ1A/XA9Uu operator@example.org" # a throwaway key, made for this test
}

run "a_t4_by_default" {
  command = plan

  assert {
    condition     = azurerm_linux_virtual_machine.inference[0].size == "Standard_NC4as_T4_v3" && length(azurerm_virtual_machine_extension.gpu_driver) == 1
    error_message = "The default is the T4 machine, with Azure's driver extension."
  }
  assert {
    condition     = strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "ExecStartPre=/usr/bin/nvidia-smi") && strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "--gpus all") && strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "text-embeddings-inference:turing-1.8")
    error_message = "The GPU machine's services wait for the driver and use the GPU, from the Turing build."
  }
  assert {
    condition     = !strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "until curl")
    error_message = "On a GPU both models start at once, as they did."
  }
  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_EMBEDDER_URL"] == "http://10.50.9.36:8080/v1" && local.retrieval_env["OPEN_ALVARY_RERANKER_URL"] == "http://10.50.9.36:8081" && local.retrieval_env["OPEN_ALVARY_RERANKER_PROFILE"] == "BAAI/bge-reranker-v2-m3@953dc6f6f85a"
    error_message = "The service reaches both models at the machine's private address."
  }
}

run "no_gpu" {
  command = plan

  variables {
    inference_gpu = "none"
  }

  assert {
    condition     = azurerm_linux_virtual_machine.inference[0].size == "Standard_D8s_v5" && length(azurerm_virtual_machine_extension.gpu_driver) == 0
    error_message = "Without a GPU, an 8-vCPU machine and no driver extension."
  }
  assert {
    condition     = !strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "nvidia") && !strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "--gpus")
    error_message = "Without a GPU, the start-up script neither waits for a driver nor installs NVIDIA's toolkit."
  }
  assert {
    condition     = strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "text-embeddings-inference:cpu-1.8.3") && strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "--max-batch-tokens 8192 --auto-truncate") && strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "--max-batch-tokens 8192 --max-client-batch-size 150")
    error_message = "Without a GPU, the CPU build serves the same models with the same limits."
  }
  assert {
    condition     = strcontains(base64decode(azurerm_linux_virtual_machine.inference[0].custom_data), "ExecStartPre=/bin/sh -c 'until curl -fs http://10.50.9.36:8080/health >/dev/null; do sleep 10; done'")
    error_message = "Without a GPU, the reranker warms up only once the embedder has."
  }
  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_EMBEDDER_URL"] == "http://10.50.9.36:8080/v1" && azurerm_network_interface.inference[0].ip_configuration[0].private_ip_address == "10.50.9.36"
    error_message = "Without a GPU, the service reaches the embedder where it did."
  }
  assert {
    condition     = !contains(keys(local.retrieval_env), "OPEN_ALVARY_RERANKER_URL") && !contains(keys(local.retrieval_env), "OPEN_ALVARY_RERANKER_PROFILE")
    error_message = "Without a GPU, the service ranks without the reranker unless reranker_on_cpu says so."
  }
}

run "no_gpu_with_its_reranker" {
  command = plan

  variables {
    inference_gpu   = "none"
    reranker_on_cpu = true
  }

  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_RERANKER_URL"] == "http://10.50.9.36:8081" && local.retrieval_env["OPEN_ALVARY_RERANKER_PROFILE"] == "BAAI/bge-reranker-v2-m3@953dc6f6f85a"
    error_message = "reranker_on_cpu points the service at the reranker on the CPUs, at the same address."
  }
}

run "no_such_gpu" {
  command = plan

  variables {
    inference_gpu = "l4"
  }

  expect_failures = [var.inference_gpu]
}
