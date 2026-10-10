provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "human-resilience-map"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# CloudFront/ALB certificates for an ALB must live in the ALB's region, so one provider suffices.
