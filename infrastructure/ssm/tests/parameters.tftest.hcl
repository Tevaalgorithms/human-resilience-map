mock_provider "aws" {}

run "parameter_shells" {
  command = plan
  assert {
    condition     = length(module.parameters.parameter_names) == 5
    error_message = "Create the five supported runtime secret parameters."
  }
  assert {
    condition     = module.parameters.parameter_names["openai-api-key"] == "/human-resilience-map/local/openai-api-key"
    error_message = "Keep the app's default OpenAI parameter path consistent."
  }
  assert {
    condition     = length(aws_iam_role_policy.reader) == 0
    error_message = "Do not modify IAM roles unless role names were provided."
  }
}

run "configured_prefix_and_roles" {
  command = plan
  variables {
    parameter_prefix  = "/human-resilience-map/prod"
    reader_role_names = { api = "fixture-api-role" }
  }
  assert {
    condition     = module.parameters.parameter_names["database-password"] == "/human-resilience-map/prod/database-password"
    error_message = "Honor the selected environment prefix."
  }
  assert {
    condition     = aws_iam_role_policy.reader["api"].role == "fixture-api-role"
    error_message = "Attach only the explicitly configured reader role."
  }
}
