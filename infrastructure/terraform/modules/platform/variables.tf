variable "name" { type = string }
variable "environment" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "frontend_security_group_id" { type = string }
variable "api_security_group_id" { type = string }
variable "jobs_security_group_id" { type = string }
variable "domain_name" { type = string }
variable "hosted_zone_id" { type = string }
variable "image" { type = string }
variable "cpu_architecture" { type = string }
variable "log_retention_days" { type = number }

variable "services" {
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
  }))
}

variable "db_host" { type = string }
variable "db_name" { type = string }
variable "db_user" { type = string }
variable "db_secret_arn" { type = string }
variable "data_bucket_name" { type = string }
variable "data_bucket_arn" { type = string }
variable "queue_url" { type = string }
variable "queue_arn" { type = string }

variable "cognito_region" { type = string }
variable "cognito_user_pool_id" { type = string }
variable "cognito_client_id" { type = string }
variable "cognito_domain_url" { type = string }
variable "cognito_client_secret" {
  type      = string
  sensitive = true
}

variable "llm_provider" { type = string }
variable "bedrock_model_id" { type = string }
variable "bedrock_model_arns" { type = list(string) }

variable "ingestion_schedule_enabled" { type = bool }
variable "ingestion_schedule_expression" { type = string }
variable "ingestion_command" { type = list(string) }
