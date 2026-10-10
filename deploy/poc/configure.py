"""Generate root-only Compose runtime configuration without printing credentials."""

import json
import os
import subprocess
from pathlib import Path

release = json.loads(Path("release.json").read_text())
# Only PostgreSQL needs host-side injection; app containers read SSM with the host IAM role.
parameter_name = release["ssm_parameter_prefix"] + "/database-password"
try:
    response = json.loads(
        subprocess.check_output(  # noqa: S603 - fixed argv, no shell
            [
                "/usr/local/bin/aws",
                "ssm",
                "get-parameters",
                "--region",
                release["region"],
                "--names",
                parameter_name,
                "--with-decryption",
                "--output",
                "json",
            ],
            text=True,
            stderr=subprocess.PIPE,
        )
    )
except (subprocess.CalledProcessError, ValueError):
    raise SystemExit(
        "Unable to read the SSM database parameter; check region, identity and permissions"
    ) from None
parameter = next((p for p in response.get("Parameters", []) if p.get("Name") == parameter_name), {})
password = parameter.get("Value", "")
if (
    parameter.get("Type") != "SecureString"
    or not password.strip()
    or password == "REPLACE_IN_AWS_CONSOLE"  # noqa: S105 - sentinel, not a secret
):
    raise SystemExit("Populate the SSM SecureString database-password before deploying")
# Compose interpolates dollar signs even inside JSON/YAML values.
env = {key: str(value).replace("$", "$$") for key, value in release["config"].items()}
env["HRM_SECRET_SOURCE"] = "ssm"  # noqa: S105 - source selector
env["HRM_SSM_PARAMETER_PREFIX"] = release["ssm_parameter_prefix"]
env["HRM_SSM_REGION"] = release["region"]
services = {
    name: {"environment": env} for name in ("api", "frontend", "worker", "dispatcher", "migrate")
}
services["db"] = {"environment": {"POSTGRES_PASSWORD": password.replace("$", "$$")}}
Path("runtime.json").write_text(json.dumps({"services": services}))
os.chmod("runtime.json", 0o600)
Path("deployment.env").write_text(
    f"APP_IMAGE={release['app_image']}\nPOSTGIS_IMAGE={release['postgis_image']}\n"
    f"APP_DOMAIN={release['domain']}\n"
)
os.chmod("deployment.env", 0o600)
