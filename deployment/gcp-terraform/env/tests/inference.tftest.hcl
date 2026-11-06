# The inference machine with a GPU and without one, planned with stand-in providers: no cloud is touched
# and nothing needs signing in. `terraform init -backend=false && terraform test`. By default it is the
# T4 machine it was; inference_gpu = none puts the same two models on CPUs, at the same address.

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "random" {}

variables {
  project_id        = "open-alvary-example"
  environment       = "staging"
  operator_email    = "operator@example.org"
  public_domain     = "open.example.org"
  api_domain        = "api.open.example.org"
  public_image      = "registry.example.org/public:1"
  workbench_image   = "registry.example.org/workbench:1"
  accounts_enabled  = true
  auth0_domain      = "login.example.org"
  console_client_id = "console"
  optional_secrets  = ["auth0-deletion-client-id", "auth0-deletion-client-secret"]
  public_settings   = {}
}

run "a_t4_by_default" {
  command = plan

  assert {
    condition     = google_compute_instance.inference[0].machine_type == "n1-standard-4" && google_compute_instance.inference[0].guest_accelerator[0].type == "nvidia-tesla-t4"
    error_message = "The default is the T4 machine."
  }
  assert {
    condition     = google_compute_instance.inference[0].boot_disk[0].initialize_params[0].image == "projects/deeplearning-platform-release/global/images/family/common-cu129-ubuntu-2204-nvidia-580"
    error_message = "A GPU machine boots the Deep Learning VM image with NVIDIA's driver."
  }
  assert {
    condition     = google_compute_instance.inference[0].scheduling[0].on_host_maintenance == "TERMINATE" && google_compute_instance.inference[0].metadata["install-nvidia-driver"] == "True"
    error_message = "A GPU machine stops for host maintenance and keeps the driver switch."
  }
  assert {
    condition     = strcontains(google_compute_instance.inference[0].metadata["user-data"], "ExecStartPre=/usr/bin/nvidia-smi") && strcontains(google_compute_instance.inference[0].metadata["user-data"], "--gpus all") && strcontains(google_compute_instance.inference[0].metadata["user-data"], "text-embeddings-inference:turing-1.8")
    error_message = "The GPU machine's services wait for the driver and use the GPU, from the Turing build."
  }
  assert {
    condition     = !strcontains(google_compute_instance.inference[0].metadata["user-data"], "until curl")
    error_message = "On a GPU both models start at once, as they did."
  }
  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_EMBEDDER_URL"] == "http://10.60.8.36:8080/v1" && local.retrieval_env["OPEN_ALVARY_RERANKER_URL"] == "http://10.60.8.36:8081" && local.retrieval_env["OPEN_ALVARY_RERANKER_PROFILE"] == "BAAI/bge-reranker-v2-m3@953dc6f6f85a"
    error_message = "The service reaches both models at the machine's private address."
  }
  assert {
    condition     = !contains(keys(local.retrieval_env), "OPEN_ALVARY_EMBEDDER_BATCH")
    error_message = "On a GPU the indexer sends its batches as it did."
  }
}

run "an_l4" {
  command = plan

  variables {
    inference_gpu = "l4"
  }

  assert {
    condition     = google_compute_instance.inference[0].machine_type == "g2-standard-4" && length(google_compute_instance.inference[0].guest_accelerator) == 0 && google_compute_instance.inference[0].scheduling[0].on_host_maintenance == "TERMINATE"
    error_message = "The L4 comes with its machine type, and the machine stops for host maintenance."
  }
  assert {
    condition     = strcontains(google_compute_instance.inference[0].metadata["user-data"], "--gpus all") && strcontains(google_compute_instance.inference[0].metadata["user-data"], "text-embeddings-inference:89-1.8")
    error_message = "The L4 machine's services use the GPU, from the Ada build."
  }
}

run "no_gpu" {
  command = plan

  variables {
    inference_gpu = "none"
  }

  assert {
    condition     = google_compute_instance.inference[0].machine_type == "n2-standard-8" && length(google_compute_instance.inference[0].guest_accelerator) == 0
    error_message = "Without a GPU, an 8-vCPU machine with no accelerator."
  }
  assert {
    condition     = google_compute_instance.inference[0].boot_disk[0].initialize_params[0].image == "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
    error_message = "Without a GPU, a plain Ubuntu image: no NVIDIA driver."
  }
  assert {
    condition     = google_compute_instance.inference[0].scheduling[0].on_host_maintenance == "MIGRATE" && !contains(keys(google_compute_instance.inference[0].metadata), "install-nvidia-driver")
    error_message = "Without a GPU, the machine moves during host maintenance and asks for no driver."
  }
  assert {
    condition     = !strcontains(google_compute_instance.inference[0].metadata["user-data"], "nvidia") && !strcontains(google_compute_instance.inference[0].metadata["user-data"], "--gpus")
    error_message = "Without a GPU, the start-up script neither waits for a driver nor installs NVIDIA's toolkit."
  }
  assert {
    condition     = strcontains(google_compute_instance.inference[0].metadata["user-data"], "text-embeddings-inference:cpu-1.8.3") && strcontains(google_compute_instance.inference[0].metadata["user-data"], "--max-batch-tokens 8192 --auto-truncate") && strcontains(google_compute_instance.inference[0].metadata["user-data"], "--max-batch-tokens 8192 --max-client-batch-size 150")
    error_message = "Without a GPU, the CPU build serves the same models with the same limits."
  }
  assert {
    condition     = strcontains(google_compute_instance.inference[0].metadata["user-data"], "ExecStartPre=/bin/sh -c 'until curl -fs http://10.60.8.36:8080/health >/dev/null; do sleep 10; done'")
    error_message = "Without a GPU, the reranker warms up only once the embedder has."
  }
  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_EMBEDDER_URL"] == "http://10.60.8.36:8080/v1" && google_compute_instance.inference[0].network_interface[0].network_ip == "10.60.8.36"
    error_message = "Without a GPU, the service reaches the embedder where it did."
  }
  assert {
    condition     = !contains(keys(local.retrieval_env), "OPEN_ALVARY_RERANKER_URL") && !contains(keys(local.retrieval_env), "OPEN_ALVARY_RERANKER_PROFILE")
    error_message = "Without a GPU, the service ranks without the reranker unless reranker_on_cpu says so."
  }
  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_EMBEDDER_BATCH"] == "1"
    error_message = "Without a GPU, the indexer sends one window a request, so a question's embedding waits behind one."
  }
}

run "no_gpu_with_its_reranker" {
  command = plan

  variables {
    inference_gpu   = "none"
    reranker_on_cpu = true
  }

  assert {
    condition     = local.retrieval_env["OPEN_ALVARY_RERANKER_URL"] == "http://10.60.8.36:8081" && local.retrieval_env["OPEN_ALVARY_RERANKER_PROFILE"] == "BAAI/bge-reranker-v2-m3@953dc6f6f85a"
    error_message = "reranker_on_cpu points the service at the reranker on the CPUs, at the same address."
  }
}

run "no_gpu_stopped" {
  command = plan

  variables {
    inference_gpu     = "none"
    inference_running = false
  }

  assert {
    condition     = google_compute_instance.inference[0].desired_status == "TERMINATED"
    error_message = "A machine without a GPU stops and starts by inference_running, as the GPU machine does."
  }
  assert {
    condition     = !contains(keys(local.retrieval_env), "OPEN_ALVARY_EMBEDDER_URL") && !contains(keys(local.retrieval_env), "OPEN_ALVARY_RERANKER_URL")
    error_message = "While it is stopped, the service is not pointed at it."
  }
}

run "no_such_gpu" {
  command = plan

  variables {
    inference_gpu = "a100"
  }

  expect_failures = [var.inference_gpu]
}
