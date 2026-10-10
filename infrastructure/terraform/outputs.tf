output "app_url" {
  value = "https://${var.domain_name}"
}

output "ecr_repository_url" {
  description = "Push the application image here (CI does this)."
  value       = module.data.ecr_repository_url
}

output "cluster_name" {
  value = module.platform.cluster_name
}

output "migrate_run_task_command" {
  description = "Run once per release (and after first apply) to apply DB migrations."
  value       = module.platform.migrate_run_task_command
}

output "ingest_run_task_command" {
  description = "Manually trigger an ingestion run."
  value       = module.platform.ingest_run_task_command
}

output "data_bucket" {
  value = module.data.data_bucket_name
}

output "queue_url" {
  value = module.data.queue_url
}

output "dead_letter_queue_url" {
  value = module.data.dlq_url
}

output "cognito_user_pool_id" {
  value = module.auth.user_pool_id
}

output "ssm_parameter_names" { value = module.platform.ssm_parameter_names }
output "ssm_parameter_prefix" { value = module.platform.ssm_parameter_prefix }
