mock_provider "aws" {}
mock_provider "random" {}
override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}
override_data {
  target = data.aws_availability_zones.available
  values = { names = ["us-west-2a", "us-west-2b"] }
}
override_data {
  target = data.aws_ssm_parameter.ubuntu
  values = { value = "ami-0123456789abcdef0" }
}
variables {
  hosted_zone_id = "Z0123456789"
  alert_email    = "owner@example.com"
}
run "poc_plan" {
  command = plan
  assert {
    condition     = aws_instance.host.instance_type == "t4g.medium"
    error_message = "POC must default to the selected Graviton instance."
  }
  assert {
    condition     = aws_instance.host.root_block_device[0].encrypted && aws_instance.host.root_block_device[0].volume_size == 30
    error_message = "POC database disk must be encrypted and start at 30 GB."
  }
  assert {
    condition     = aws_instance.host.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 must be required."
  }
  assert {
    condition     = aws_instance.host.credit_specification[0].cpu_credits == "standard"
    error_message = "Surplus CPU-credit billing must be disabled by default."
  }
}
