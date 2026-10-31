import os
import re
import subprocess
from datetime import datetime, timezone


def release_metadata(ref: str, run_id: str) -> dict[str, str]:
    tag = (
        ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else ""
    )
    if tag:
        match = re.fullmatch(
            r"v\d+\.\d+(?:\.\d+)?(?P<pre>-(?:a|b|rc|dev)\.\d+)?", tag
        )
        if not match:
            raise ValueError("Release tags must use vYY.MM.N[-dev.N] format.")
        version = tag[1:]
        prerelease = bool(match.group("pre"))
    else:
        now = datetime.now(timezone.utc)
        version = f"{now.year % 100}.{now.month}.0-dev.{int(run_id)}"
        prerelease = True
    return {
        "tag": tag,
        "version": version,
        "prerelease": "true" if prerelease else "",
    }


def reject_existing_versions(
    version: str, image: str, charts: str, chart_prefix: str
) -> None:
    lookups: list[tuple[list[str], str]] = []
    if image:
        reference = f"ghcr.io/neuro-inc/{image}:{version}"
        lookups.append(
            (
                ["docker", "buildx", "imagetools", "inspect", reference],
                reference,
            )
        )
    for chart in charts.replace(",", " ").split():
        repository = f"ghcr.io/neuro-inc/helm-charts{chart_prefix}/{chart}"
        lookups.append(
            (
                [
                    "helm",
                    "show",
                    "chart",
                    f"oci://{repository}",
                    "--version",
                    version,
                ],
                f"{repository}:{version}",
            )
        )
    for command, reference in lookups:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            raise RuntimeError(
                f"Published release already exists: {reference}"
            )
        error = (result.stdout + result.stderr).strip().lower()
        missing = {
            f"{reference}: not found",
            f"error: {reference}: not found",
            f"no such manifest: {reference}",
        }
        repository, tag = reference.rsplit(":", 1)
        manifest_url = (
            f"https://ghcr.io/v2/{repository.removeprefix('ghcr.io/')}/"
            f"manifests/{tag}"
        )
        missing.add(
            "error: unexpected status from head request to "
            f"{manifest_url}: 404 not found"
        )
        if error not in missing:
            raise RuntimeError(f"Cannot verify release absence: {reference}")


def main() -> None:
    metadata = release_metadata(
        os.environ["GITHUB_REF"], os.environ["GITHUB_RUN_ID"]
    )
    reject_existing_versions(
        metadata["version"],
        os.environ["IMAGE"],
        os.environ["CHARTS"],
        os.environ["CHART_PREFIX"],
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in metadata.items():
            print(f"{key}={value}", file=output)


if __name__ == "__main__":
    main()
