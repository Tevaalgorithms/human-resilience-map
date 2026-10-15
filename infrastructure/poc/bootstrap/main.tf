terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 5.80" } }
}
variable "region" {
  type    = string
  default = "us-west-2"
}
variable "github_repository" {
  type        = string
  description = "GitHub owner/repository, using the default OIDC subject format."
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use owner/repository."
  }
}
variable "hosted_zone_id" {
  type        = string
  description = "Only this existing Route 53 hosted zone can be modified by CI."
}
variable "aws_account_id" {
  type        = string
  description = "Terraform refuses to run against any other AWS account (12 digits)."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Use the 12-digit AWS account ID."
  }
}
variable "name" {
  type    = string
  default = "hrm-poc"
}
variable "existing_oidc_provider_arn" {
  type        = string
  default     = null
  description = "Reuse an existing GitHub OIDC provider if this account already has one."
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.aws_account_id]
}
data "aws_caller_identity" "current" {}
locals {
  account          = data.aws_caller_identity.current.account_id
  bucket           = "${var.name}-state-${local.account}-${var.region}"
  state_bucket_arn = "arn:aws:s3:::${local.bucket}"
  oidc             = coalesce(var.existing_oidc_provider_arn, try(aws_iam_openid_connect_provider.github[0].arn, null))
}
resource "aws_s3_bucket" "state" {
  bucket = local.bucket
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.existing_oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-github-deploy"
  max_session_duration = 7200
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Federated = local.oidc }, Action = "sts:AssumeRoleWithWebIdentity",
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:ref:refs/heads/main"
    } }
  }] })
}
# Provisioning needs account-level create/describe permissions. Scope service actions;
# keep identity management, secret values, DNS writes, artifacts and SSM targeted.
resource "aws_iam_role_policy" "deploy" {
  role = aws_iam_role.deploy.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = [
      "ec2:Describe*", "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
      "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
      "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway", "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
      "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:ReplaceRoute",
      "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:ModifySecurityGroupRules",
      "ec2:RunInstances", "ec2:TerminateInstances", "ec2:StartInstances", "ec2:StopInstances", "ec2:ModifyInstanceAttribute",
      "ec2:ModifyInstanceCreditSpecification", "ec2:ModifyInstanceMetadataOptions", "ec2:ModifyVolume",
      "ec2:AllocateAddress", "ec2:ReleaseAddress", "ec2:AssociateAddress", "ec2:DisassociateAddress",
      "ec2:CreateTags", "ec2:DeleteTags", "ec2:GetSecurityGroupsForVpc"
    ], Resource = "*" },
    { Effect = "Allow", Action = ["iam:CreateRole", "iam:GetRole", "iam:UpdateAssumeRolePolicy", "iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:CreateInstanceProfile", "iam:GetInstanceProfile", "iam:DeleteInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile", "iam:UntagInstanceProfile", "iam:ListInstanceProfilesForRole"], Resource = ["arn:aws:iam::${local.account}:role/${var.name}-host", "arn:aws:iam::${local.account}:instance-profile/${var.name}-host"] },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = "arn:aws:iam::${local.account}:role/${var.name}-host", Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
    { Effect = "Allow", Action = ["iam:CreateServiceLinkedRole"], Resource = "arn:aws:iam::${local.account}:role/aws-service-role/cognito-idp.amazonaws.com/*", Condition = { StringEquals = { "iam:AWSServiceName" = "cognito-idp.amazonaws.com" } } },
    { Effect = "Allow", Action = ["s3:*"], Resource = [local.state_bucket_arn, "${local.state_bucket_arn}/*", "arn:aws:s3:::${var.name}-${local.account}-${var.region}", "arn:aws:s3:::${var.name}-${local.account}-${var.region}/*"] },
    { Effect = "Allow", Action = ["ecr:*"], Resource = "arn:aws:ecr:${var.region}:${local.account}:repository/${var.name}/*" },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["cognito-idp:CreateUserPool", "cognito-idp:ListUserPools"], Resource = "*" },
    { Effect = "Allow", Action = ["cognito-idp:DescribeUserPool", "cognito-idp:UpdateUserPool", "cognito-idp:DeleteUserPool", "cognito-idp:TagResource", "cognito-idp:UntagResource", "cognito-idp:ListTagsForResource", "cognito-idp:CreateUserPoolClient", "cognito-idp:DescribeUserPoolClient", "cognito-idp:UpdateUserPoolClient", "cognito-idp:DeleteUserPoolClient", "cognito-idp:CreateUserPoolDomain", "cognito-idp:DescribeUserPoolDomain", "cognito-idp:DeleteUserPoolDomain"], Resource = "arn:aws:cognito-idp:${var.region}:${local.account}:userpool/*" },
    { Effect = "Allow", Action = ["secretsmanager:CreateSecret", "secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret", "secretsmanager:DeleteSecret", "secretsmanager:TagResource", "secretsmanager:UntagResource", "secretsmanager:GetResourcePolicy"], Resource = "arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.name}/*" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource"], Resource = "arn:aws:logs:${var.region}:${local.account}:log-group:/${var.name}/*" },
    { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "*" },
    { Effect = "Allow", Action = ["route53:GetHostedZone", "route53:ListResourceRecordSets", "route53:ChangeResourceRecordSets", "route53:ListTagsForResource"], Resource = "arn:aws:route53:::hostedzone/${var.hosted_zone_id}" },
    { Effect = "Allow", Action = ["route53:GetChange"], Resource = "arn:aws:route53:::change/*" },
    { Effect = "Allow", Action = ["budgets:ViewBudget", "budgets:ModifyBudget", "budgets:DescribeBudget", "budgets:DescribeNotificationsForBudget", "budgets:DescribeSubscribersForNotification", "budgets:CreateBudget", "budgets:UpdateBudget", "budgets:DeleteBudget", "budgets:CreateNotification", "budgets:UpdateNotification", "budgets:DeleteNotification", "budgets:CreateSubscriber", "budgets:DeleteSubscriber", "budgets:TagResource", "budgets:UntagResource", "budgets:ListTagsForResource"], Resource = "*" },
    { Effect = "Allow", Action = ["ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters", "ssm:DeleteParameter", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource"], Resource = "arn:aws:ssm:${var.region}:${local.account}:parameter/${var.name}/*" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.region}::parameter/aws/service/canonical/*" },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "arn:aws:ssm:${var.region}::document/AWS-RunShellScript" },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "arn:aws:ec2:${var.region}:${local.account}:instance/*", Condition = { StringEquals = { "ssm:resourceTag/Project" = "human-resilience-map" } } },
    { Effect = "Allow", Action = ["ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"], Resource = "*" }
  ] })
}
output "deploy_role_arn" { value = aws_iam_role.deploy.arn }
output "state_bucket" { value = aws_s3_bucket.state.id }
