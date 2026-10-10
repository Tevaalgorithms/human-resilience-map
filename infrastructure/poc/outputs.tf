output "instance_id" { value = aws_instance.host.id }
output "public_ip" { value = aws_eip.host.public_ip }
output "app_url" { value = "https://${var.domain_name}" }
output "data_bucket" { value = aws_s3_bucket.data.id }
output "app_repository" { value = aws_ecr_repository.images["app"].repository_url }
output "postgis_repository" { value = aws_ecr_repository.images["postgis"].repository_url }
output "runtime_secret_arn" { value = aws_secretsmanager_secret.runtime.arn }
output "runtime_config" {
  value = {
    HRM_ENVIRONMENT          = "prod"
    HRM_SECRET_SOURCE        = "ssm"
    HRM_SSM_PARAMETER_PREFIX = "/${var.name}"
    HRM_SSM_REGION           = var.region
    HRM_AUTH_MODE            = "cognito"
    HRM_PUBLIC_BASE_URL      = "https://${var.domain_name}"
    HRM_COGNITO_REGION       = var.region
    HRM_COGNITO_USER_POOL_ID = module.auth.user_pool_id
    HRM_COGNITO_CLIENT_ID    = module.auth.client_id
    HRM_COGNITO_DOMAIN       = module.auth.domain_url
    HRM_AWS_REGION           = var.region
    HRM_S3_BUCKET            = aws_s3_bucket.data.id
    HRM_OBJECT_STORE_BACKEND = "s3"
    HRM_QUEUE_BACKEND        = "local"
    HRM_LLM_PROVIDER         = "stub"
    HRM_API_BASE_URL         = "http://api:8000"
    HRM_DB_HOST              = "db"
    HRM_DB_NAME              = "resilience"
    HRM_DB_USER              = "resilience"
    WEB_CONCURRENCY          = "1"
  }
}

output "ssm_parameter_names" { value = module.ssm.parameter_names }
output "ssm_parameter_prefix" { value = "/${var.name}" }
