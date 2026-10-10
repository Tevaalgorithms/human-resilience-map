locals {
  name = "${var.name}-${var.environment}"
}

module "network" {
  source             = "./modules/network"
  name               = local.name
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
  log_retention_days = var.log_retention_days
}

module "data" {
  source             = "./modules/data"
  name               = local.name
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  client_security_group_ids = [
    module.network.frontend_security_group_id,
    module.network.api_security_group_id,
    module.network.jobs_security_group_id,
  ]
  db_instance_class           = var.db_instance_class
  db_allocated_storage_gb     = var.db_allocated_storage_gb
  db_max_allocated_storage_gb = var.db_max_allocated_storage_gb
  db_multi_az                 = var.db_multi_az
  db_backup_retention_days    = var.db_backup_retention_days
  db_deletion_protection      = var.db_deletion_protection
}

module "auth" {
  source        = "./modules/auth"
  name          = local.name
  callback_urls = ["https://${var.domain_name}/callback"]
  logout_urls   = ["https://${var.domain_name}"]
}

module "platform" {
  source = "./modules/platform"

  name               = local.name
  environment        = var.environment
  region             = var.region
  vpc_id             = module.network.vpc_id
  public_subnet_ids  = module.network.public_subnet_ids
  private_subnet_ids = module.network.private_subnet_ids

  frontend_security_group_id = module.network.frontend_security_group_id
  api_security_group_id      = module.network.api_security_group_id
  jobs_security_group_id     = module.network.jobs_security_group_id

  domain_name    = var.domain_name
  hosted_zone_id = var.hosted_zone_id

  image              = "${module.data.ecr_repository_url}:${var.image_tag}"
  cpu_architecture   = var.cpu_architecture
  services           = var.services
  log_retention_days = var.log_retention_days

  db_host          = module.data.db_host
  db_name          = module.data.db_name
  db_user          = module.data.db_user
  db_secret_arn    = module.data.db_master_secret_arn
  data_bucket_name = module.data.data_bucket_name
  data_bucket_arn  = module.data.data_bucket_arn
  queue_url        = module.data.queue_url
  queue_arn        = module.data.queue_arn

  cognito_region        = var.region
  cognito_user_pool_id  = module.auth.user_pool_id
  cognito_client_id     = module.auth.client_id
  cognito_client_secret = module.auth.client_secret
  cognito_domain_url    = module.auth.domain_url

  llm_provider       = var.llm_provider
  bedrock_model_id   = var.bedrock_model_id
  bedrock_model_arns = var.bedrock_model_arns

  ingestion_schedule_enabled    = var.ingestion_schedule_enabled
  ingestion_schedule_expression = var.ingestion_schedule_expression
  ingestion_command             = var.ingestion_command
}

module "observability" {
  source = "./modules/observability"

  name                    = local.name
  alert_email             = var.alert_email
  monthly_budget_usd      = var.monthly_budget_usd
  dlq_name                = module.data.dlq_name
  queue_name              = module.data.queue_name
  db_identifier           = module.data.db_identifier
  alb_arn_suffix          = module.platform.alb_arn_suffix
  target_group_arn_suffix = module.platform.frontend_target_group_arn_suffix
  cluster_name            = module.platform.cluster_name
  worker_service_name     = module.platform.worker_service_name
}
