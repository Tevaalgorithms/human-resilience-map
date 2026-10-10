variable "region" {
  description = "AWS region. Open decision in ADR-001: confirm Bedrock model availability here."
  type        = string
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "name" {
  description = "Resource name prefix."
  type        = string
  default     = "hrm"
}

# --- DNS / TLS ---
variable "domain_name" {
  description = "Public FQDN for the application (e.g. heat.example.org)."
  type        = string
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone that contains domain_name."
  type        = string
}

# --- Network ---
variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of AZs for subnets (ALB requires >= 2)."
  type        = number
  default     = 2
}

variable "single_nat_gateway" {
  description = "One shared NAT (cheaper, single-AZ egress) vs one per AZ."
  type        = bool
  default     = true
}

# --- Database (open decision: confirm class/backups after load testing) ---
variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 50
}

variable "db_max_allocated_storage_gb" {
  type    = number
  default = 200
}

variable "db_multi_az" {
  description = "Pilot default is Single-AZ (no production availability). Enable only after defining availability needs."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 7
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

# --- Compute ---
variable "image_tag" {
  description = "Container image tag in the ECR repo (CI publishes the git SHA)."
  type        = string
}

variable "cpu_architecture" {
  description = "Must match the architecture the image was built for."
  type        = string
  default     = "X86_64"
  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "services" {
  description = "Per-service sizing. Worker desired_count is the job concurrency limit; size from measured GIS/solver runtime and memory."
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
  }))
  default = {
    frontend   = { cpu = 512, memory = 1024, desired_count = 1 }
    api        = { cpu = 512, memory = 1024, desired_count = 1 }
    worker     = { cpu = 1024, memory = 4096, desired_count = 1 }
    dispatcher = { cpu = 256, memory = 512, desired_count = 1 }
  }
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# --- LLM ---
variable "llm_provider" {
  description = "openai, bedrock or explicit offline stub. Populate SSM before enabling live providers."
  type        = string
  default     = "stub"
  validation {
    condition     = contains(["openai", "stub", "bedrock"], var.llm_provider)
    error_message = "llm_provider must be openai, stub or bedrock."
  }
}

variable "bedrock_model_id" {
  description = "Bedrock model/inference-profile id; required when llm_provider = bedrock."
  type        = string
  default     = ""
}

variable "bedrock_model_arns" {
  description = "ARNs the worker may invoke. Tighten to the selected model/inference profile."
  type        = list(string)
  default     = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*"]
}

# --- Ingestion schedule ---
variable "ingestion_schedule_enabled" {
  description = "Enable only after a manual refresh has been verified (checklist step 13)."
  type        = bool
  default     = false
}

variable "ingestion_schedule_expression" {
  type    = string
  default = "cron(0 6 ? * MON *)"
}

variable "ingestion_command" {
  description = "Arguments passed to the `ingest` entrypoint."
  type        = list(string)
  default     = ["--source", "files"]
}

# --- Cost / alerting ---
variable "alert_email" {
  description = "Email subscribed to alarm and budget notifications."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Planning envelope from docs/COST_ESTIMATE.md (not a spending authorization)."
  type        = number
  default     = 310
}
