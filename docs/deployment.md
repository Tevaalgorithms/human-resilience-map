# Deploying to AWS (checklist steps 12–13)

Everything is in `infrastructure/terraform`. **Nothing here has been applied**: it was validated with
`terraform validate` only, no AWS account was touched. Review the first `terraform plan` carefully.

## Resources

VPC (public/private subnets, one NAT, S3 gateway endpoint, REJECT flow logs) · RDS PostgreSQL 16
(encrypted, forced TLS, managed master secret, Single-AZ by default) · private S3 data bucket
(versioned, TLS-only) · SQS queue + dead-letter queue · ECR (immutable tags, scan on push) ·
ECS Fargate services: **frontend** (behind the HTTPS ALB), **api** (private, Cloud Map DNS),
**worker**, **dispatcher** · one-off **migrate** and scheduled **ingest** tasks (EventBridge Scheduler,
disabled by default) · ACM certificate + Route 53 record · Cognito user pool (invite-only, MFA optional)
· SSM SecureString parameters (manual values), retained RDS/legacy Secrets Manager resources · CloudWatch alarms (DLQ, queue age, 5xx, unhealthy hosts, DB CPU/storage, worker
memory) · AWS Budget with email alerts.

## Steps

1. **Decide the open items** in `docs/pilot_scope.md` and `environments/pilot.tfvars` (region, domain,
   hosted zone, DB class, alert email, Bedrock model).
2. **Bootstrap state** (once per account):
   `cd infrastructure/terraform/bootstrap && terraform init && terraform apply -var region=us-east-1`,
   then put the bucket name in `environments/pilot.backend.hcl`.
3. **Create the registry first** so an image exists before the services start:
   ```bash
   cd infrastructure/terraform
   terraform init -backend-config=environments/pilot.backend.hcl
   terraform apply -var-file=environments/pilot.tfvars -target=module.data.aws_ecr_repository.app
   ```
4. **Build and push the image** (the CI `image` job does this on `main`; manually):
   ```bash
   aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
   docker build --platform linux/amd64 -t <ecr_repository_url>:<git-sha> . && docker push <ecr_repository_url>:<git-sha>
   ```
   Set `image_tag` in the tfvars (tags are immutable).
5. **Apply everything**: `terraform apply -var-file=environments/pilot.tfvars`. Certificate validation can
   take several minutes.
6. **Populate SSM**: use the `ssm_parameter_names` output and manually replace placeholders in
   Parameter Store. The database password must match the RDS database user; the Cognito secret
   must match the app client. Supply a strong session secret and provider API keys as required.
   Initial service tasks remain unready until these values exist. After populating them, force a
   new deployment/restart, then **migrate the database**: run the `migrate_run_task_command` output. Check the task's CloudWatch
   log group `/<name>/migrate`. (Migration 0001 runs `CREATE EXTENSION postgis`; the RDS master user can.)
7. **Create a planner**: Cognito console → user pool → Create user (invite-only).
8. **Publish the first dataset**: put the source files (see `ingestion/sources.py::REQUIRED_FILES`) in the
   data bucket / build a real adapter, run `ingest_run_task_command`. The `files` source currently reads a
   local directory; a real pilot needs an adapter for your data (checklist step 4 → 5).
9. **Verify** the full flow, then failure paths: stop the worker mid-job, send a duplicate message, try
   another user's job id, and restore an RDS snapshot to a scratch instance. Document rollback (re-apply the
   previous `image_tag`; ECS circuit breaker auto-rolls back failed deployments).
10. Enable the schedule (`ingestion_schedule_enabled = true`) only after a manual refresh is verified.

## Operating notes

- **Job concurrency** = worker `desired_count` (each worker runs one job at a time). Size CPU/memory from
  measured GIS/solver runs; the worker-memory alarm warns before OOM.
- **Costs**: the NAT gateway and RDS are the always-on baseline. Recalculate `docs/COST_ESTIMATE.md` for the
  region/DB class you choose; the budget alert uses `monthly_budget_usd`.
- **Bedrock**: set `llm_provider = "bedrock"`, `bedrock_model_id`, and narrow `bedrock_model_arns`.
- **Not included yet**: WAF on the ALB, KMS customer-managed keys, VPC interface endpoints (add if NAT data
  charges dominate), RDS Proxy, autoscaling, cross-region backups. Add them per the production review.

## Low-cost Graviton POC: automatic deployment from main

This alternative lives in `infrastructure/poc`, separate from the existing ECS/RDS stack.
It creates a t4g.medium, 30 GB encrypted gp3 root disk, Elastic IP, one public subnet/VPC,
HTTP/HTTPS security group (no SSH), private S3 bucket, two ECR repositories, Cognito login,
one runtime secret, CloudWatch agent/log group, Route 53 subdomain record, IAM roles and
budget notifications. There is no NAT gateway, ALB, RDS, ECS or paid LangGraph hosting.
The database, queue, checkpoint store and sessions share PostgreSQL/PostGIS on the host.

The default is `https://resilience.tevaalgorithms.com`, us-west-2 (Oregon), your AWS account (set `aws_account_id`), and a $50 monthly budget.
Planning cost: EC2 $24.53, EBS $2.40, IPv4 $3.65, plus small S3/ECR, monitoring, secrets and
DNS usage. Allow $40–$50/month before model calls, taxes, significant transfer or increased
backup retention. ECR and Terraform state add small storage costs. Standard CPU-credit mode
avoids surplus-credit charges but can throttle sustained analysis workloads. No compute free
trial is assumed for t4g.medium. OpenAI is supported for the local/Compose app (see [AI model setup](ai_models.md)); this legacy EC2 example still uses its explicit offline configuration: deployed
LangGraph uses the existing deterministic `stub` until a provider adapter is added.

### One-time AWS bootstrap

Use Terraform 1.10+ and authenticated AWS administrator/provisioning credentials locally.
Run commands from the project directory (the directory containing pyproject.toml).

```bash
terraform -chdir=infrastructure/poc/bootstrap init
terraform -chdir=infrastructure/poc/bootstrap plan \
  -var='github_repository=OWNER/REPOSITORY' \
  -var='hosted_zone_id=YOUR_EXISTING_ZONE_ID' -out=bootstrap.tfplan
terraform -chdir=infrastructure/poc/bootstrap apply bootstrap.tfplan
terraform -chdir=infrastructure/poc/bootstrap output
```

If the account already has the GitHub OIDC provider, also supply
`-var='existing_oidc_provider_arn=arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com'`
to the plan command. The bootstrap creates the state bucket and a deployment role trusted only
by this repository's `main` branch, using GitHub's default OIDC subject format. Repositories
using a customized OIDC subject must adjust the trust policy. No GitHub Environment is attached
to the deployment job, because that would change the subject claim.

Protect the local bootstrap state: it manages the deployment identity and remote-state bucket.
Do not commit state or plan files. Application Terraform state is encrypted/versioned in S3
and locked through S3 lockfiles. It contains generated database/session/Cognito secrets;
restrict access accordingly. The deployment role can provision EC2 networking across this
account and manage Cognito pools in its region; use a dedicated POC account when possible.
DNS writes are limited to the selected hosted zone. Infrastructure auto-apply is authorized
by merging reviewed code into main; require PR review and successful checks through branch
protection. GitHub OIDC cannot bootstrap its own initial trusted AWS role.

### GitHub repository variables

In Settings → Secrets and variables → Actions → Variables, configure:

| Variable | Value |
| --- | --- |
| `POC_DEPLOY_ROLE_ARN` | Bootstrap `deploy_role_arn` output |
| `POC_STATE_BUCKET` | Bootstrap `state_bucket` output |
| `POC_HOSTED_ZONE_ID` | Existing authoritative Route 53 zone ID |
| `POC_ALERT_EMAIL` | Your budget-notification email |
| `AWS_REGION` | `us-west-2` (optional; default) |
| `POC_DOMAIN_NAME` | `resilience.tevaalgorithms.com` (optional; default) |

Deploy settings assume the bootstrap resource name `hrm-poc`. Ensure the subdomain is not
already managed by another Terraform stack and Route 53 is authoritative for the domain.
Activate the `Project` cost-allocation tag in AWS Billing so the project-filtered budget can
track expenses; tag activation can take time. Budget alerts do not enforce a spending cap.
Invite the first user in Cognito after deployment; public self-registration is disabled.

### Pull request and main behavior

`.github/workflows/deploy-poc.yml` performs credential-free Terraform validation/mock-plan tests,
Python checks, native ARM application/PostGIS builds and database integration tests on PRs.
Merging a PR into main (a push to main) runs those checks again, assumes the AWS role through
OIDC, plans/applies the POC stack, publishes the tested images to ECR, and uploads a release
bundle to S3. SSM downloads it on EC2, fetches secrets locally, backs up the database, runs
migrations and starts the containers. Import validated actual source files separately; no fictional seeding runs.
It checks internal health and public HTTPS before reporting success. A manual workflow dispatch
on main is also supported. PRs never assume the deployment role or apply infrastructure.

If this project is a subfolder of a larger repository, GitHub only discovers workflows in the
repository-root `.github/workflows`: copy these workflows there. The new POC workflow detects
both project-root and `human-resilience-map/` layouts. The existing CI also detects both layouts. Configure branch protection to require the validation and ARM jobs. Native ARM GitHub-hosted
runners require a repository/plan that supports them; private-repository Actions usage can
incur charges separately from AWS.
No GitHub push, PR creation, merge or AWS provisioning is performed by adding these files.

### Host operations and recovery

Ubuntu ARM64, Docker/Compose, AWS CLI, SSM Agent and CloudWatch Agent are installed by cloud-init.
Caddy terminates HTTPS; only ports 80 and 443 are published. Persistent database/certificate
volumes survive deployments. Container logs rotate locally (10 MB × 3 per container); only
bootstrap logs are sent to CloudWatch, with seven-day retention. Memory/disk custom metrics
are sampled every five minutes. Operational logs can be inspected with Session Manager and
`docker compose logs` in `/opt/hrm/current`. Daily backups run through `hrm-backup.timer` and
expire in S3 after 14 days. Backup failures are visible in the systemd journal; notification
alarms are not configured. Application export responses are served by FastAPI; raw ingestion
snapshots are stored in S3 under `raw/`.

The instance has Terraform `prevent_destroy` and its root disk is retained on termination.
AMI updates are ignored to prevent automatic database-host replacement; patch the OS in place.
Changes to cloud-init do not re-run installation on an existing host. Apply required host
updates explicitly through Session Manager. Resizing needs a short outage. Infrastructure
replacement or database-password rotation must be planned: changing the secret alone does
not change the existing PostgreSQL role password.

There is no automatic database rollback. A deployment failure may leave services stopped;
inspect the SSM command, fix the issue, and re-run the workflow. To roll back application code,
revert the commit through a new PR, then deploy it; schema changes must remain compatible.
Before an incompatible schema rollback, restore a backup into a separate temporary database
and verify it, then schedule a deliberate recovery. Never run `docker compose down -v` on the
POC host. Test a restore before relying on the backups. Load-test a full analysis and map load
together before opening access; the specified memory limits are initial budgets, not capacity
measurements. POC availability is single-host and maintenance/failures cause downtime.

## Selected initial target (2026-10 update)

The initial deployment decision is now **4 GB Lightsail, Docker Compose, $30–40/month estimated
budget target**. Follow [the Lightsail setup/recovery path](lightsail.md) and
`deploy/lightsail/compose.yml`. It does not use EC2 Terraform, SSM release automation or instance
roles. Existing EC2 POC and ECS/RDS instructions above remain alternatives, with their own costs
and infrastructure assumptions. No AWS provisioning or deployment was performed for this change.

Application credentials are retrieved from SSM with task roles. Production rejects local environment-only secrets. See [SSM setup and rotation requirements](ssm_parameters.md).
