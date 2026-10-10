data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---- Secrets ----
resource "random_password" "session" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "session" {
  name                    = "${var.name}/session-secret"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "session" {
  secret_id     = aws_secretsmanager_secret.session.id
  secret_string = random_password.session.result
}

resource "aws_secretsmanager_secret" "cognito_client" {
  name                    = "${var.name}/cognito-client-secret"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "cognito_client" {
  secret_id     = aws_secretsmanager_secret.cognito_client.id
  secret_string = var.cognito_client_secret
}

# ---- Execution role (pull image, write logs, read ONLY the secrets injected into tasks) ----
resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      var.db_secret_arn,
      aws_secretsmanager_secret.session.arn,
      aws_secretsmanager_secret.cognito_client.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# ---- Task roles: least privilege per workload ----
locals {
  task_roles = toset(["frontend", "api", "worker", "dispatcher", "jobs"])
}

resource "aws_iam_role" "task" {
  for_each           = local.task_roles
  name               = "${var.name}-task-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# frontend and api: database + auth only (no AWS API permissions needed).

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "ConsumeQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
    resources = [var.queue_arn]
  }
  statement {
    sid       = "ArtifactsReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.data_bucket_arn}/*"]
  }
  dynamic "statement" {
    for_each = var.llm_provider == "bedrock" ? [1] : []
    content {
      sid       = "InvokeSelectedModel"
      actions   = ["bedrock:InvokeModel", "bedrock:Converse"]
      resources = var.bedrock_model_arns
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.task["worker"].id
  policy = data.aws_iam_policy_document.worker.json
}

data "aws_iam_policy_document" "dispatcher" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [var.queue_arn]
  }
}

resource "aws_iam_role_policy" "dispatcher" {
  role   = aws_iam_role.task["dispatcher"].id
  policy = data.aws_iam_policy_document.dispatcher.json
}

# migrate + ingest tasks
data "aws_iam_policy_document" "jobs" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [var.data_bucket_arn, "${var.data_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "jobs" {
  role   = aws_iam_role.task["jobs"].id
  policy = data.aws_iam_policy_document.jobs.json
}

# App-side SSM retrieval uses task roles, not the image-pull execution role.
module "ssm" {
  source           = "../ssm"
  parameter_prefix = "/${var.name}"
}
resource "aws_iam_role_policy" "ssm" {
  for_each = aws_iam_role.task
  name     = "${var.name}-ssm-${each.key}"
  role     = each.value.id
  policy   = module.ssm.reader_policies[each.key == "jobs" ? "ingest" : each.key]
}
output "ssm_parameter_names" { value = module.ssm.parameter_names }
output "ssm_parameter_prefix" { value = "/${var.name}" }
