terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state: supply bucket/key/region via environments/<env>.backend.hcl
  #   terraform init -backend-config=environments/pilot.backend.hcl
  backend "s3" {}
}
