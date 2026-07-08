"""lark-agent CDK stacks package."""

import os
import subprocess
from typing import Optional

from aws_cdk import BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
import jsii

_RETENTION_MAP = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def retention_days(days: int) -> logs.RetentionDays:
    """Convert an integer number of days to the nearest valid RetentionDays enum."""
    if days in _RETENTION_MAP:
        return _RETENTION_MAP[days]
    for d in sorted(_RETENTION_MAP):
        if d >= days:
            return _RETENTION_MAP[d]
    return logs.RetentionDays.ONE_YEAR


@jsii.implements(ILocalBundling)
class _UvLocalBundling:
    """Install a Lambda's requirements + source into the asset output with uv.

    Avoids requiring Docker at synth. Uses `uv pip install` with an explicit
    aarch64 target platform so native wheels (e.g. cryptography) match the ARM64
    Lambda runtime regardless of the build host.
    """

    def __init__(self, source_dir: str):
        self.source_dir = source_dir

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ANN001
        req = os.path.join(self.source_dir, "requirements.txt")
        # copy source files (.py)
        for name in os.listdir(self.source_dir):
            if name.endswith(".py"):
                _copy(os.path.join(self.source_dir, name), os.path.join(output_dir, name))
        if os.path.exists(req):
            subprocess.check_call([
                "uv", "pip", "install",
                "-r", req, "--target", output_dir,
                "--python-platform", "aarch64-manylinux2014",
                "--python-version", "3.13",
                "--only-binary=:all:",
            ])
        return True


def _copy(src: str, dst: str) -> None:
    import shutil
    shutil.copy2(src, dst)


def lambda_asset(source_dir: str) -> _lambda.AssetCode:
    """Bundle a Lambda source dir (source + pip deps) into an asset.

    If the dir has no requirements.txt, this is a plain source asset. Docker is
    the CDK-default bundling image but local bundling is attempted first.
    """
    return _lambda.Code.from_asset(
        source_dir,
        bundling=BundlingOptions(
            image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.13:latest"),
            local=_UvLocalBundling(source_dir),
            command=[
                "bash", "-c",
                "cp -r /asset-input/*.py /asset-output/ 2>/dev/null || true; "
                "if [ -f /asset-input/requirements.txt ]; then "
                "pip install -r /asset-input/requirements.txt -t /asset-output; fi",
            ],
        ),
    )
