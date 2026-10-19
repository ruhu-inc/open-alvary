# The public service: one Cloud Run service serving the website, the docs, REST v1, MCP and the
# developer console (api.service), as today. It is reached only through the load balancer (edge.tf):
# its ingress takes internal and load-balancer traffic only and its run.app address is off, so its own
# address is no way around Cloud Armor. ALVARY_FRONT_DOOR_ID is therefore never set here: that check is
# Azure's way of closing the same door. Its private traffic (Cloud SQL, the engine, the models) goes
# into the network by Direct VPC egress. Its jobs share its image, identity and settings (jobs.tf).

locals {
  public_enabled = var.public_image != ""

  # Settings shared by the service and its jobs, as deployment/gcp/public_service.sh gives them today.
  public_env = merge(
    {
      ALVARY_RUNTIME_MODE          = "persistent"
      PUBLIC_DOMAIN                = var.public_domain
      ALVARY_SITE_ORIGIN           = "https://${var.public_domain}"
      ALVARY_API_ORIGIN            = "https://${var.api_domain}"
      ALVARY_FEED_KEY_ID           = var.feed_key_id
      ALVARY_WEBHOOK_KEY_ID        = var.webhook_key_id
      ALVARY_ADMISSION_CONFIG      = "/secrets/admission/config.json"
      ALVARY_SITE_ADMISSION_CONFIG = "/secrets/site-admission/config.json"
    },
    var.public_settings,
    local.retrieval_env,
    var.accounts_enabled ? {
      ALVARY_ACCOUNTS          = "1"
      ALVARY_CONSOLE_ISSUER    = "https://${trimsuffix(trimprefix(var.auth0_domain, "https://"), "/")}/"
      ALVARY_CONSOLE_CLIENT_ID = var.console_client_id
      ALVARY_CONSOLE_BASE_URL  = "https://${var.public_domain}"
    } : {},
  )

  # Environment variable → Secret Manager secret.
  public_secret_env = merge(
    {
      DATABASE_URL        = "database-url"
      ALVARY_FEED_KEYS    = "feed-keys"
      ALVARY_WEBHOOK_KEYS = "webhook-keys"
    },
    var.accounts_enabled ? {
      ALVARY_KEY_PEPPER            = "key-pepper"
      ALVARY_CONSENT_SECRET        = "consent-secret"
      ALVARY_CONSOLE_CLIENT_SECRET = "console-client-secret"
      RESEND_API_KEY               = "resend-api-key"
    } : {},
    { for name in var.optional_secrets : local.optional_secret_env[name] => name },
  )
  optional_secret_env = {
    "resend-webhook-secret"        = "RESEND_WEBHOOK_SECRET"
    "auth0-deletion-client-id"     = "ALVARY_AUTH0_DELETION_CLIENT_ID"
    "auth0-deletion-client-secret" = "ALVARY_AUTH0_DELETION_CLIENT_SECRET"
  }

  # The admission configurations are read as files: one secret volume each (Cloud Run mounts one
  # secret per folder), at the paths the settings above name.
  public_secret_files = {
    admission      = "admission-config"
    site-admission = "site-admission-config"
  }
}

resource "google_cloud_run_v2_service" "public" {
  count                = local.public_enabled ? 1 : 0
  name                 = "${local.name_prefix}-public"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  default_uri_disabled = true # no run.app address: the load balancer is the only way in
  invoker_iam_disabled = true # the public site: anyone the load balancer lets through
  deletion_protection  = false

  template {
    service_account                  = google_service_account.public.email
    max_instance_request_concurrency = 80

    scaling {
      min_instance_count = var.public_min_replicas
      max_instance_count = var.public_max_replicas
    }

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      name  = "public"
      image = var.public_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.public_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.public_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      dynamic "volume_mounts" {
        for_each = local.public_secret_files
        content {
          name       = volume_mounts.key
          mount_path = "/secrets/${volume_mounts.key}"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds    = 5
        failure_threshold = 24
      }
      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds = 30
      }
    }

    dynamic "volumes" {
      for_each = local.public_secret_files
      content {
        name = volumes.key
        secret {
          secret = volumes.value
          items {
            path    = "config.json"
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.readers, google_secret_manager_secret_version.generated]
}
