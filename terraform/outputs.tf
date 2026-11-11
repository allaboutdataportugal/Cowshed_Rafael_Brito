output "api_url" {
  description = "Base URL of the API."
  value       = "http://localhost:${var.api_port}"
}

output "api_docs_url" {
  description = "Interactive OpenAPI documentation."
  value       = "http://localhost:${var.api_port}/docs"
}

output "reports_dir" {
  description = "Host folder that receives the daily reports."
  value       = local.reports_dir
}

output "psql_command" {
  description = "Open a SQL shell inside the database container."
  value       = "docker exec -it ${docker_container.db.name} psql -U cowshed -d cowshed"
}

output "database_url" {
  description = "SQLAlchemy URL used by the containers (contains the generated password)."
  value       = local.database_url
  sensitive   = true
}
