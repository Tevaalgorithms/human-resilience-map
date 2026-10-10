"""Send a bounded SSM deployment and report failure without logging runtime secrets."""

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path


def aws(*args):
    return json.loads(
        subprocess.check_output(  # noqa: S603 - fixed AWS argv, no shell
            [shutil.which("aws") or "/usr/local/bin/aws", *args, "--output", "json"], text=True
        )
    )


release = json.loads(Path("deploy/poc/release.json").read_text())
instance = os.environ["INSTANCE_ID"]
revision = os.environ["GITHUB_SHA"]
root = f"/opt/hrm/releases/{revision}"
uri = f"s3://{release['bucket']}/releases/{revision}.tar.gz"
# Give a fresh host time to register with SSM before sending commands.
for _ in range(60):
    nodes = aws(
        "ssm",
        "describe-instance-information",
        "--filters",
        json.dumps(
            [
                {"Key": "InstanceIds", "Values": [instance]},
            ]
        ),
    )["InstanceInformationList"]
    if nodes and nodes[0]["PingStatus"] == "Online":
        break
    time.sleep(10)
else:
    raise SystemExit("EC2 did not register with SSM within ten minutes")
commands = [
    "set -eu",
    "cloud-init status --wait",
    f"install -d -m 0700 {shlex.quote(root)}",
    f"aws s3 cp {shlex.quote(uri)} /tmp/hrm-release.tar.gz --only-show-errors",
    f"tar -xzf /tmp/hrm-release.tar.gz -C {shlex.quote(root)}",
    f"bash {shlex.quote(root + '/deploy.sh')}",
]
response = aws(
    "ssm",
    "send-command",
    "--instance-ids",
    instance,
    "--document-name",
    "AWS-RunShellScript",
    "--timeout-seconds",
    "600",
    "--parameters",
    json.dumps({"commands": commands, "executionTimeout": ["1200"]}),
)
command = response["Command"]["CommandId"]
for _ in range(150):
    try:
        result = aws(
            "ssm", "get-command-invocation", "--command-id", command, "--instance-id", instance
        )
    except subprocess.CalledProcessError:
        time.sleep(10)
        continue
    if result["Status"] == "Success":
        print("Deployment completed; internal health checks passed.")
        break
    if result["Status"] in {"Failed", "Cancelled", "TimedOut"}:
        # SSM output can contain third-party error details; inspect in AWS with privileged access.
        raise SystemExit(f"Deployment {result['Status']}; inspect SSM command {command} in AWS")
    time.sleep(10)
else:
    raise SystemExit(f"Deployment polling timed out; inspect SSM command {command}")
