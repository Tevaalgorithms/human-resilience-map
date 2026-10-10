# terraform init -backend-config=environments/pilot.backend.hcl
bucket       = "REPLACE-tfstate-bucket" # created by ./bootstrap
key          = "human-resilience-map/pilot/terraform.tfstate"
region       = "us-east-1"
encrypt      = true
use_lockfile = true # S3-native locking (Terraform >= 1.10); use dynamodb_table on older versions
