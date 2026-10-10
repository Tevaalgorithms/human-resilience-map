data "aws_caller_identity" "current" {}

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE"]
}

locals {
  api_base_url = "http://api.${aws_service_discovery_private_dns_namespace.this.name}:8000"

  common_env = {
    HRM_ENVIRONMENT          = var.environment
    HRM_SECRET_SOURCE        = "ssm"
    HRM_SSM_PARAMETER_PREFIX = "/${var.name}"
    HRM_SSM_REGION           = var.region
    HRM_LOG_LEVEL            = "INFO"
    HRM_DB_HOST              = var.db_host
    HRM_DB_NAME              = var.db_name
    HRM_DB_USER              = var.db_user
    HRM_DB_SSLMODE           = "require"
    HRM_AUTH_MODE            = "cognito"
    HRM_COGNITO_REGION       = var.cognito_region
    HRM_COGNITO_USER_POOL_ID = var.cognito_user_pool_id
    HRM_COGNITO_CLIENT_ID    = var.cognito_client_id
    HRM_COGNITO_DOMAIN       = var.cognito_domain_url
    HRM_PUBLIC_BASE_URL      = "https://${var.domain_name}"
    HRM_API_BASE_URL         = local.api_base_url
    HRM_QUEUE_BACKEND        = "sqs"
    HRM_SQS_QUEUE_URL        = var.queue_url
    HRM_SQS_REGION           = var.region
    HRM_OBJECT_STORE_BACKEND = "s3"
    HRM_S3_BUCKET            = var.data_bucket_name
    HRM_AWS_REGION           = var.region
    HRM_LLM_PROVIDER         = var.llm_provider
    HRM_BEDROCK_MODEL_ID     = var.bedrock_model_id
    HRM_BEDROCK_REGION       = var.region
  }

  # Runtime credentials are read from SSM using the service's task role.
  # Existing Secrets Manager resources are retained for a reviewed migration/cleanup.

  python_health = "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:%d/%s',timeout=3)\""

  # One image, many roles. `role` selects the IAM task role; `command` is the entrypoint argument.
  tasks = {
    frontend = {
      role   = "frontend", command = ["frontend"], port = 8050
      health = format(local.python_health, 8050, "healthz"), stop = 30
      cpu    = var.services["frontend"].cpu, memory = var.services["frontend"].memory
    }
    api = {
      role   = "api", command = ["api"], port = 8000
      health = format(local.python_health, 8000, "health"), stop = 30
      cpu    = var.services["api"].cpu, memory = var.services["api"].memory
    }
    worker = {
      role = "worker", command = ["worker"], port = 0, health = "", stop = 120 # finish/yield job on SIGTERM
      cpu  = var.services["worker"].cpu, memory = var.services["worker"].memory
    }
    dispatcher = {
      role = "dispatcher", command = ["dispatcher"], port = 0, health = "", stop = 30
      cpu  = var.services["dispatcher"].cpu, memory = var.services["dispatcher"].memory
    }
    migrate = {
      role = "jobs", command = ["migrate"], port = 0, health = "", stop = 30
      cpu  = 512, memory = 1024
    }
    ingest = {
      role = "jobs", command = concat(["ingest"], var.ingestion_command), port = 0, health = "", stop = 30
      cpu  = 1024, memory = 4096
    }
  }
}

resource "aws_cloudwatch_log_group" "task" {
  for_each          = local.tasks
  name              = "/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "task" {
  for_each                 = local.tasks
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task[each.value.role].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([{
    name                   = each.key
    image                  = var.image
    essential              = true
    command                = each.value.command
    stopTimeout            = each.value.stop
    readonlyRootFilesystem = false # Dash/matplotlib-style caches; revisit with a tmpfs volume
    portMappings           = each.value.port > 0 ? [{ containerPort = each.value.port, protocol = "tcp" }] : []
    environment            = [for k, v in local.common_env : { name = k, value = v }]
    secrets                = []
    healthCheck = each.value.health == "" ? null : {
      command     = ["CMD-SHELL", each.value.health]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

# ---------------- Long-running services ----------------
resource "aws_ecs_service" "frontend" {
  name            = "${var.name}-frontend"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.task["frontend"].arn
  desired_count   = var.services["frontend"].desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.frontend_security_group_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 8050
  }

  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "api" {
  name            = "${var.name}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.task["api"].arn
  desired_count   = var.services["api"].desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.api_security_group_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.api.arn
  }
}

resource "aws_ecs_service" "background" {
  for_each        = toset(["worker", "dispatcher"])
  name            = "${var.name}-${each.key}"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.task[each.key].arn
  desired_count   = var.services[each.key].desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.jobs_security_group_id]
    assign_public_ip = false
  }
}

# ---------------- Scheduled ingestion (disabled until a manual refresh is verified) ----------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["${aws_ecs_task_definition.task["ingest"].arn_without_revision}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task["jobs"].arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "ingest" {
  name                = "${var.name}-ingest"
  schedule_expression = var.ingestion_schedule_expression
  state               = var.ingestion_schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.task["ingest"].arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [var.jobs_security_group_id]
        assign_public_ip = false
      }
    }

    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 3600
    }
  }
}

locals {
  run_task_base = "aws ecs run-task --region ${var.region} --cluster ${aws_ecs_cluster.this.name} --launch-type FARGATE --network-configuration 'awsvpcConfiguration={subnets=[${join(",", var.private_subnet_ids)}],securityGroups=[${var.jobs_security_group_id}],assignPublicIp=DISABLED}'"
}

output "cluster_name" { value = aws_ecs_cluster.this.name }
output "alb_arn_suffix" { value = aws_lb.this.arn_suffix }
output "frontend_target_group_arn_suffix" { value = aws_lb_target_group.frontend.arn_suffix }
output "worker_service_name" { value = aws_ecs_service.background["worker"].name }
output "migrate_run_task_command" {
  value = "${local.run_task_base} --task-definition ${aws_ecs_task_definition.task["migrate"].family}"
}
output "ingest_run_task_command" {
  value = "${local.run_task_base} --task-definition ${aws_ecs_task_definition.task["ingest"].family}"
}
