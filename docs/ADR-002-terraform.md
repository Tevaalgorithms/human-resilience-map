# ADR-002: Terraform for AWS infrastructure

Date: October 6, 2026 · Status: Accepted (supersedes the infrastructure-as-code part of ADR-001 decision 7)

## Context
ADR-001 proposed Python AWS CDK. The project owner asked for Terraform.

## Decision
Define all AWS infrastructure in Terraform (`infrastructure/terraform`), as small modules
(`network`, `data`, `auth`, `platform`, `observability`) with per-environment `tfvars` and S3 remote
state. The architecture in ADR-001 is otherwise unchanged.

## Consequences
- HCL joins Python, SQL and Docker as a maintained language; CDK's ability to share Python types with
  the app is lost (acceptable: the app reads plain environment variables).
- Reviewable plans in CI (`terraform fmt`, `validate`; add `plan` once an AWS role is configured).
- Secrets: the RDS master password is managed by RDS in Secrets Manager. The Cognito client secret and
  generated session secret pass through Terraform state, so the state bucket must stay private,
  encrypted and access-controlled (the bootstrap module does this).
- ADR-001 should be updated when convenient (checklist step 14).
