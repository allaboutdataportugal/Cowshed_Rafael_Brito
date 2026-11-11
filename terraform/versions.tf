terraform {
  required_version = ">= 1.5.0"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Talks to the local Docker engine (Docker Desktop on Windows/macOS, dockerd on Linux).
# Leave docker_host empty to use DOCKER_HOST or the platform default.
provider "docker" {
  host = var.docker_host != "" ? var.docker_host : null
}
