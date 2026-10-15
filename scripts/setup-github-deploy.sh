#!/usr/bin/env bash
# One-time setup for the Deploy to Lightsail workflow. Run it yourself: it changes IAM.
#   AWS_PROFILE=<admin> ./scripts/setup-github-deploy.sh
# It creates (if missing) the GitHub OIDC provider and a role that can only edit the firewall of
# Lightsail instances in us-west-2. No long-lived AWS keys are stored in GitHub.
set -euo pipefail
REPO="Tevaalgorithms/human-resilience-map"
# GitHub's OIDC subject now embeds the immutable owner and repository IDs (shown by the
# deploy workflow's "Show OIDC subject" step). Both forms are accepted.
REPO_ID_SUB="repo:Tevaalgorithms@45029995/human-resilience-map@1411241901:environment:production"
ROLE="hrm-github-deploy"
REGION="us-west-2"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
OIDC="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"

aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC" >/dev/null 2>&1 ||
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null

TRUST="$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"$OIDC"},
 "Action":"sts:AssumeRoleWithWebIdentity",
 "Condition":{"StringEquals":{"token.actions.githubusercontent.com:aud":"sts.amazonaws.com",
 "token.actions.githubusercontent.com:sub":["repo:${REPO}:environment:production","${REPO_ID_SUB}"]}}}]}
JSON
)"
POLICY="$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Action":["lightsail:PutInstancePublicPorts","lightsail:GetInstancePortStates","lightsail:GetInstance"],
 "Resource":"arn:aws:lightsail:${REGION}:${ACCOUNT}:Instance/*"}]}
JSON
)"
aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" >/dev/null 2>&1 ||
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$TRUST"
aws iam put-role-policy --role-name "$ROLE" --policy-name lightsail-firewall --policy-document "$POLICY"

cat <<MSG

Role ready: arn:aws:iam::${ACCOUNT}:role/${ROLE}

Add these as repository SECRETS, not variables (this repo is public; secrets stay masked in logs). Also create an environment named
"production" (Settings > Environments) and add yourself as a required reviewer:
  Secret   DEPLOY_ROLE_ARN   = arn:aws:iam::${ACCOUNT}:role/${ROLE}
  Secret   LIGHTSAIL_HOST    = <static IP of hrm-prod>
  Secret   ADMIN_CIDR        = <your admin IP>/32   (currently the host's SSH rule)
  Secret   LIGHTSAIL_SSH_KEY = contents of a deploy-only private key (see docs/deployment.md)
  Secret   LIGHTSAIL_KNOWN_HOSTS = output of: ssh-keyscan -t ed25519 <static IP>
MSG
