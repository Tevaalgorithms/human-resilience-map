variable "name" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "client_security_group_ids" { type = list(string) }
variable "db_instance_class" { type = string }
variable "db_allocated_storage_gb" { type = number }
variable "db_max_allocated_storage_gb" { type = number }
variable "db_multi_az" { type = bool }
variable "db_backup_retention_days" { type = number }
variable "db_deletion_protection" { type = bool }

variable "queue_visibility_timeout_s" {
  description = "Must exceed the worker lease/heartbeat interval; workers extend it for long jobs."
  type        = number
  default     = 300
}

variable "queue_max_receive_count" {
  description = "Deliveries before a message moves to the dead-letter queue (matches HRM_QUEUE_MAX_RECEIVE_COUNT)."
  type        = number
  default     = 3
}

data "aws_caller_identity" "current" {}

# ---------------- Database: RDS PostgreSQL (PostGIS enabled by migration 0001) ----------------
resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL access from application tiers only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_clients" {
  count                        = length(var.client_security_group_ids)
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = var.client_security_group_ids[count.index]
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name  = "resilience"
  username = "resilience"
  # RDS creates and rotates the master password in Secrets Manager; it never appears in code.
  manage_master_user_password = true

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false

  backup_retention_period    = var.db_backup_retention_days
  backup_window              = "07:00-08:00"
  maintenance_window         = "sun:08:30-sun:09:30"
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true

  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name}-final"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = false
  apply_immediately               = false

  lifecycle {
    ignore_changes = [engine_version]
  }
}

# ---------------- Object storage ----------------
resource "aws_s3_bucket" "data" {
  bucket = "${var.name}-data-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }

  rule {
    id     = "raw-snapshots-infrequent-access"
    status = "Enabled"
    filter { prefix = "raw/" }
    transition {
      days          = 60
      storage_class = "STANDARD_IA"
    }
  }
}

data "aws_iam_policy_document" "data_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "data" {
  bucket     = aws_s3_bucket.data.id
  policy     = data.aws_iam_policy_document.data_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.data]
}

# ---------------- Queue + dead-letter queue ----------------
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-analysis-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "analysis" {
  name                       = "${var.name}-analysis"
  visibility_timeout_seconds = var.queue_visibility_timeout_s
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 10
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.queue_max_receive_count
  })
}

# ---------------- Container registry ----------------
resource "aws_ecr_repository" "app" {
  name                 = "resilience-map"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 30 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 }
      action       = { type = "expire" }
    }]
  })
}

output "db_host" { value = aws_db_instance.this.address }
output "db_name" { value = aws_db_instance.this.db_name }
output "db_user" { value = aws_db_instance.this.username }
output "db_identifier" { value = aws_db_instance.this.identifier }
output "db_master_secret_arn" { value = aws_db_instance.this.master_user_secret[0].secret_arn }
output "data_bucket_name" { value = aws_s3_bucket.data.bucket }
output "data_bucket_arn" { value = aws_s3_bucket.data.arn }
output "queue_url" { value = aws_sqs_queue.analysis.url }
output "queue_arn" { value = aws_sqs_queue.analysis.arn }
output "queue_name" { value = aws_sqs_queue.analysis.name }
output "dlq_url" { value = aws_sqs_queue.dlq.url }
output "dlq_name" { value = aws_sqs_queue.dlq.name }
output "ecr_repository_url" { value = aws_ecr_repository.app.repository_url }
