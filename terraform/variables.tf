variable "docker_host" {
  description = "Docker engine address. Empty = DOCKER_HOST / platform default (e.g. npipe:////./pipe/docker_engine)."
  type        = string
  default     = ""
}

variable "project" {
  description = "Prefix for every container, network and volume."
  type        = string
  default     = "cowshed"
}

variable "api_port" {
  description = "Host port the API is published on."
  type        = number
  default     = 8000
}

variable "db_port" {
  description = "Host port PostgreSQL is published on (for psql / BI tools). 0 = not published."
  type        = number
  default     = 5432
}

variable "api_workers" {
  description = "Uvicorn worker processes in the API container."
  type        = number
  default     = 4

  validation {
    condition     = var.api_workers >= 1 && var.api_workers <= 32
    error_message = "api_workers must be between 1 and 32."
  }
}

variable "report_timezone" {
  description = "IANA timezone defining the farm's calendar day for reports."
  type        = string
  default     = "UTC"
}

variable "report_time" {
  description = "Local time (HH:MM) at which yesterday's report is generated."
  type        = string
  default     = "01:00"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.report_time))
    error_message = "report_time must be HH:MM (24h)."
  }
}

variable "reports_dir" {
  description = "Host folder where the daily reports are written. Empty = <repo>/reports."
  type        = string
  default     = ""
}

variable "reports_uid" {
  description = "User id the scheduler runs as, so it can write into reports_dir. On Linux pass your own: -var reports_uid=$(id -u)."
  type        = string
  default     = "10001"
}

variable "run_seed" {
  description = "Start the simulator once to replay the parquet datasets into the API."
  type        = bool
  default     = false
}

variable "cow_data_dir" {
  description = "Host folder with cows.parquet, sensors.parquet and measurements.parquet (needed when run_seed = true)."
  type        = string
  default     = ""
}

variable "seed_concurrency" {
  description = "Requests the simulator keeps in flight."
  type        = number
  default     = 32
}
