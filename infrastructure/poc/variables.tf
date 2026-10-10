variable "region" {
  type    = string
  default = "us-west-2"
}
variable "aws_account_id" {
  type        = string
  default     = "702018061088"
  description = "Terraform refuses to run against any other AWS account."
}
variable "name" {
  type    = string
  default = "hrm-poc"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "Use a lowercase resource prefix, 3–25 characters."
  }
}
variable "domain_name" {
  type    = string
  default = "resilience.tevaalgorithms.com"
}
variable "hosted_zone_id" { type = string }
variable "alert_email" { type = string }
variable "instance_type" {
  type    = string
  default = "t4g.medium"
  validation {
    condition     = contains(["t4g.small", "t4g.medium", "t4g.large"], var.instance_type)
    error_message = "Choose a supported ARM64 T4g instance."
  }
}
variable "volume_size_gb" {
  type    = number
  default = 30
  validation {
    condition     = var.volume_size_gb >= 30
    error_message = "At least 30 GB is required for container images and database storage."
  }
}
variable "monthly_budget_usd" {
  type    = number
  default = 50
}
variable "availability_zone" {
  type        = string
  default     = null
  description = "Keep fixed after deployment to avoid relocating the server."
}
