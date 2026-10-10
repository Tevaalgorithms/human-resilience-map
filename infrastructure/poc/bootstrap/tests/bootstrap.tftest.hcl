mock_provider "aws" {}
override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}
variables {
  github_repository          = "example/resilience-map"
  hosted_zone_id             = "Z0123456789"
  existing_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
}
run "bootstrap_plan" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.deploy.policy) <= 10240
    error_message = "Deployment role inline policy must fit the IAM size limit."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:example/resilience-map:ref:refs/heads/main"
    error_message = "GitHub AWS access must be restricted to this repository's main branch."
  }
}
