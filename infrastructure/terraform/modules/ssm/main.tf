variable "parameter_prefix" {
  type        = string
  description = "Environment-specific SSM path, without a trailing slash."
  validation {
    condition     = can(regex("^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", var.parameter_prefix))
    error_message = "Use an absolute SSM path without a trailing slash."
  }
}

variable "kms_key_arn" {
  type        = string
  default     = null
  description = "Optional customer-managed KMS key ARN; null uses AWS's aws/ssm key."
  validation {
    condition     = var.kms_key_arn == null ? true : can(regex("^arn:[^:]+:kms:[^:]+:[0-9]+:key/.+$", var.kms_key_arn))
    error_message = "Supply a KMS key ARN, not an alias."
  }
}

locals {
  parameters = {
    "openai-api-key"        = "OpenAI API key; manually replace the placeholder before enabling OpenAI."
    "census-api-key"        = "Census API key; manually replace before enabling Census preparation."
    "database-password"     = "Password matching the application's database user."
    "session-secret"        = "Strong random application session secret."
    "cognito-client-secret" = "Secret matching the deployed Cognito app client."
  }
  service_parameters = {
    frontend   = ["database-password", "session-secret", "cognito-client-secret"]
    api        = ["database-password", "session-secret", "openai-api-key", "census-api-key"]
    worker     = ["database-password", "session-secret", "openai-api-key"]
    dispatcher = ["database-password", "session-secret"]
    migrate    = ["database-password", "session-secret"]
    ingest     = ["database-password", "session-secret", "census-api-key"]
  }
}

resource "aws_ssm_parameter" "secret" {
  for_each    = local.parameters
  name        = "${var.parameter_prefix}/${each.key}"
  description = each.value
  type        = "SecureString"
  tier        = "Standard"
  key_id      = var.kms_key_arn
  value       = "REPLACE_IN_AWS_CONSOLE"

  # Operators own values; Terraform owns parameter names, encryption and metadata.
  lifecycle {
    ignore_changes  = [value]
    prevent_destroy = true
  }
}

output "parameter_names" {
  value = { for key, parameter in aws_ssm_parameter.secret : key => parameter.name }
}
output "parameter_arns" {
  value = { for key, parameter in aws_ssm_parameter.secret : key => parameter.arn }
}
output "reader_policies" {
  description = "Least-privilege read policies per app service; no secret values."
  value = { for service, keys in local.service_parameters : service => jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = [for key in keys : aws_ssm_parameter.secret[key].arn]
      }
      ], var.kms_key_arn == null ? [] : [
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.kms_key_arn]
      }
    ])
  }) }
}
