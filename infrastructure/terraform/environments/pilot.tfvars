# Limited-access pilot. Values marked TODO are open decisions in ADR-001 / checklist step 12.
region      = "us-east-1" # TODO: confirm region against Bedrock model availability
environment = "prod"      # "prod" enables ALB deletion protection; pilot is still Single-AZ

domain_name    = "heat.example.org" # TODO
hosted_zone_id = "Z0000000000000"   # TODO
alert_email    = "ops@example.org"  # TODO

image_tag = "REPLACE_WITH_GIT_SHA"

db_instance_class        = "db.t4g.small" # TODO: confirm after load-testing representative GIS jobs
db_multi_az              = false
db_backup_retention_days = 7

llm_provider     = "stub" # switch to "bedrock" once a model is selected and evaluated
bedrock_model_id = ""

monthly_budget_usd         = 310
ingestion_schedule_enabled = false
