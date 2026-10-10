region      = "us-east-1"
environment = "dev"

domain_name    = "heat-dev.example.org" # TODO
hosted_zone_id = "Z0000000000000"       # TODO
alert_email    = "dev@example.org"      # TODO

image_tag = "REPLACE_WITH_GIT_SHA"

db_instance_class           = "db.t4g.micro"
db_allocated_storage_gb     = 20
db_max_allocated_storage_gb = 50
db_backup_retention_days    = 1
db_deletion_protection      = false

services = {
  frontend   = { cpu = 256, memory = 512, desired_count = 1 }
  api        = { cpu = 256, memory = 512, desired_count = 1 }
  worker     = { cpu = 1024, memory = 2048, desired_count = 1 }
  dispatcher = { cpu = 256, memory = 512, desired_count = 1 }
}

llm_provider       = "stub"
monthly_budget_usd = 150
log_retention_days = 7
