"""Generate a private PostgreSQL env file from SSM; application containers read SSM directly."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    config = dict(
        line.split("=", 1)
        for line in Path("runtime.env").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    name = config["HRM_SSM_PARAMETER_PREFIX"] + "/database-password"
    aws = shutil.which("aws")
    if not aws:
        raise SystemExit("Install AWS CLI before generating the SSM database configuration")
    try:
        response = json.loads(
            subprocess.check_output(  # noqa: S603 - fixed argv, no shell
                [
                    aws,
                    "ssm",
                    "get-parameters",
                    "--region",
                    config["HRM_SSM_REGION"],
                    "--names",
                    name,
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
            "Unable to read SSM database parameter; check AWS identity, region and permissions"
        ) from None
    parameter = next((p for p in response.get("Parameters", []) if p.get("Name") == name), {})
    value = parameter.get("Value", "")
    if (
        parameter.get("Type") != "SecureString"
        or not value.strip()
        or value == "REPLACE_IN_AWS_CONSOLE"
    ):
        raise SystemExit("Populate SSM database-password before deploying")
    if "\n" in value or "\r" in value:
        raise SystemExit("Database password cannot contain a line break for this deployment format")
    # Create with restrictive permissions before writing any secret bytes.
    fd = os.open("database.env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("POSTGRES_PASSWORD=" + value + "\n")
    print("Created private database.env from SSM; no credential values were printed.")


if __name__ == "__main__":
    main()
