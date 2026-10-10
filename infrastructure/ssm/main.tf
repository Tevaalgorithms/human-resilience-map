# Standalone: creates SSM parameter shells and optional IAM read policies only.
# Does not deploy or modify the application, database, or Cognito client.
terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.80" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "human-resilience-map", Environment = var.environment, ManagedBy = "terraform" }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}
variable "environment" {
  type    = string
  default = "local"
}
variable "parameter_prefix" {
  type    = string
  default = "/human-resilience-map/local"
}
variable "kms_key_arn" {
  type    = string
  default = null
}
variable "reader_role_names" {
  type        = map(string)
  default     = {}
  description = "Optional service => existing IAM role name. No IAM roles are created."
  validation {
    condition     = alltrue([for service in keys(var.reader_role_names) : contains(["frontend", "api", "worker", "dispatcher", "migrate", "ingest"], service)])
    error_message = "Use frontend, api, worker, dispatcher, migrate or ingest as service keys."
  }
}

module "parameters" {
  source           = "../terraform/modules/ssm"
  parameter_prefix = var.parameter_prefix
  kms_key_arn      = var.kms_key_arn
}

resource "aws_iam_role_policy" "reader" {
  for_each = var.reader_role_names
  name     = "hrm-ssm-${each.key}"
  role     = each.value
  policy   = module.parameters.reader_policies[each.key]
}

output "parameter_names" { value = module.parameters.parameter_names }
output "parameter_arns" { value = module.parameters.parameter_arns }
output "reader_policies" { value = module.parameters.reader_policies }
