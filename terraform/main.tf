# =============================================================================
# Cowshed - local infrastructure as code
#
#   network  cowshed-net
#   volume   cowshed-pgdata     (PostgreSQL data, survives restarts and re-applies)
#   image    cowshed:local      (built from ../Dockerfile, rebuilt when src/ changes)
#   cowshed-db         PostgreSQL 16, schema + views loaded from ../sql/init
#   cowshed-api        FastAPI, published on localhost:<api_port>
#   cowshed-scheduler  writes yesterday's report every day to <reports_dir>
#   cowshed-seed       optional one-shot simulator (run_seed = true)
# =============================================================================

locals {
  repo_root   = abspath("${path.module}/..")
  reports_dir = var.reports_dir != "" ? abspath(var.reports_dir) : "${local.repo_root}/reports"

  # Rebuild the image only when something that goes into it changes.
  image_inputs = concat(
    [for f in fileset("${local.repo_root}/src", "**/*.py") : filesha1("${local.repo_root}/src/${f}")],
    [filesha1("${local.repo_root}/pyproject.toml"), filesha1("${local.repo_root}/Dockerfile")],
  )

  database_url = "postgresql+psycopg://cowshed:${random_password.db.result}@${var.project}-db:5432/cowshed"

  app_env = [
    "COWSHED_DATABASE_URL=${local.database_url}",
    "COWSHED_REPORT_TIMEZONE=${var.report_timezone}",
    "COWSHED_REPORT_DIR=/reports",
  ]
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "docker_network" "this" {
  name = "${var.project}-net"
}

resource "docker_volume" "pgdata" {
  name = "${var.project}-pgdata"

  lifecycle {
    prevent_destroy = false # set to true for anything that holds real data
  }
}

# --- database -----------------------------------------------------------------
resource "docker_image" "postgres" {
  name         = "postgres:16.4-alpine"
  keep_locally = true
}

resource "docker_container" "db" {
  name    = "${var.project}-db"
  image   = docker_image.postgres.image_id
  restart = "unless-stopped"

  env = [
    "POSTGRES_DB=cowshed",
    "POSTGRES_USER=cowshed",
    "POSTGRES_PASSWORD=${random_password.db.result}",
  ]

  networks_advanced {
    name = docker_network.this.id
  }

  volumes {
    volume_name    = docker_volume.pgdata.name
    container_path = "/var/lib/postgresql/data"
  }

  # Schema and views, executed by the postgres entrypoint on the first start only.
  dynamic "upload" {
    for_each = fileset("${local.repo_root}/sql/init", "*.sql")
    content {
      file    = "/docker-entrypoint-initdb.d/${upload.value}"
      content = file("${local.repo_root}/sql/init/${upload.value}")
    }
  }

  dynamic "ports" {
    for_each = var.db_port > 0 ? [var.db_port] : []
    content {
      internal = 5432
      external = ports.value
    }
  }

  healthcheck {
    test     = ["CMD-SHELL", "pg_isready -U cowshed -d cowshed"]
    interval = "5s"
    timeout  = "5s"
    retries  = 20
  }

  wait         = true
  wait_timeout = 120
}

# --- application image ------------------------------------------------------
resource "docker_image" "app" {
  name = "${var.project}:local"

  build {
    context    = local.repo_root
    dockerfile = "Dockerfile"
  }

  triggers = {
    inputs = sha1(join(",", local.image_inputs))
  }
}

resource "docker_container" "api" {
  name    = "${var.project}-api"
  image   = docker_image.app.image_id
  restart = "unless-stopped"
  env     = local.app_env

  command = [
    "uvicorn", "--factory", "cowshed.api:create_app",
    "--host", "0.0.0.0", "--port", "8000", "--workers", tostring(var.api_workers),
  ]

  networks_advanced {
    name = docker_network.this.id
  }

  ports {
    internal = 8000
    external = var.api_port
  }

  healthcheck {
    test     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
    interval = "5s"
    timeout  = "5s"
    retries  = 20
  }

  wait         = true
  wait_timeout = 120

  depends_on = [docker_container.db]
}

resource "docker_container" "scheduler" {
  name    = "${var.project}-scheduler"
  image   = docker_image.app.image_id
  restart = "unless-stopped"
  env     = local.app_env
  command = ["cowshed", "schedule", "--at", var.report_time, "--publish", "/reports"]
  user    = var.reports_uid

  networks_advanced {
    name = docker_network.this.id
  }

  volumes {
    host_path      = local.reports_dir
    container_path = "/reports"
  }

  depends_on = [docker_container.api]
}

# --- optional: replay the provided datasets -----------------------------------
resource "docker_container" "seed" {
  count = var.run_seed ? 1 : 0

  name     = "${var.project}-seed"
  image    = docker_image.app.image_id
  restart  = "no"
  must_run = false
  command = [
    "cowshed", "simulate", "--data-dir", "/data",
    "--api-url", "http://${var.project}-api:8000", "--concurrency", tostring(var.seed_concurrency),
  ]

  networks_advanced {
    name = docker_network.this.id
  }

  volumes {
    host_path      = abspath(var.cow_data_dir)
    container_path = "/data"
    read_only      = true
  }

  lifecycle {
    precondition {
      condition     = var.cow_data_dir != "" && fileexists("${var.cow_data_dir}/measurements.parquet")
      error_message = "run_seed = true needs cow_data_dir pointing at the folder with the three parquet files."
    }
  }

  depends_on = [docker_container.api]
}
