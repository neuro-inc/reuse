"""Remove expired temporary image graphs from explicit GHCR packages."""

import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote

DIGEST = r"sha256:[0-9a-f]{64}"


def text(value: object, pattern: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("The cleanup input contains an invalid identity.")
    return value


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=60).strip()


def gh(*args: str) -> str:
    return command("gh", *args)


def package_api(repository: str) -> str:
    text(repository, r"ghcr\.io/[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9._/-]*")
    _, owner, package = repository.split("/", 2)
    return f"orgs/{owner}/packages/container/{quote(package, safe='')}"


def protected_digests(path: Path) -> dict[str, set[str]]:
    def unique_packages(items: list[tuple[str, object]]) -> dict[str, object]:
        if len({key for key, _ in items}) != len(items):
            raise ValueError("Protected packages must be unique.")
        return dict(items)

    value: object = json.loads(
        path.read_text(), object_pairs_hook=unique_packages
    )
    if not isinstance(value, dict) or not value:
        raise ValueError(
            "Protected digests must name at least one GHCR package."
        )
    protected: dict[str, set[str]] = {}
    for repository, digests in value.items():
        package_api(repository)
        if not isinstance(digests, list):
            raise ValueError("Each package must have a protected digest list.")
        protected[repository] = {text(digest, DIGEST) for digest in digests}
    return protected


def registry_digest(reference: str) -> str:
    manifest: object = json.loads(
        command(
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest}}",
        )
    )
    if not isinstance(manifest, dict):
        raise ValueError("The registry returned invalid image metadata.")
    return text(manifest.get("digest"), DIGEST)


class Version(TypedDict):
    id: int
    digest: str
    tags: list[str]
    updated: datetime


def package_versions(repository: str) -> list[Version]:
    pages: object = json.loads(
        gh(
            "api",
            f"{package_api(repository)}/versions?per_page=100",
            "--paginate",
            "--slurp",
        )
    )
    if not isinstance(pages, list):
        raise ValueError("GitHub must return package version pages.")
    versions: list[Version] = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("GitHub must return a package version list.")
        for item in page:
            if (
                not isinstance(item, dict)
                or type(item.get("id")) is not int
                or item["id"] <= 0
            ):
                raise ValueError("GitHub returned an invalid package version.")
            metadata = item.get("metadata")
            container = (
                metadata.get("container")
                if isinstance(metadata, dict)
                else None
            )
            tags = (
                container.get("tags") if isinstance(container, dict) else None
            )
            if not isinstance(tags, list) or not all(
                isinstance(tag, str) for tag in tags
            ):
                raise ValueError("GitHub returned invalid container tags.")
            updated = item.get("updated_at")
            if not isinstance(updated, str):
                raise ValueError(
                    "GitHub returned an invalid package timestamp."
                )
            timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("Package timestamps must include a timezone.")
            versions.append(
                {
                    "id": item["id"],
                    "digest": text(item.get("name"), DIGEST),
                    "tags": tags,
                    "updated": timestamp,
                }
            )
    if len({v["digest"] for v in versions}) != len(versions):
        raise ValueError("GitHub returned duplicate image digests.")
    return versions


def children(repository: str, digest: str) -> set[str]:
    manifest: object = json.loads(
        command(
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            repository + "@" + digest,
        )
    )
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ValueError("The registry returned an unsupported manifest.")
    descriptors = manifest.get("manifests")
    if descriptors is None:
        if not isinstance(manifest.get("config"), dict) or not isinstance(
            manifest.get("layers"), list
        ):
            raise ValueError(
                "The registry returned an invalid image manifest."
            )
        return set()
    if not isinstance(descriptors, list):
        raise ValueError("The registry returned an invalid image index.")
    result: set[str] = set()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise ValueError("The image index contains an invalid descriptor.")
        result.add(text(descriptor.get("digest"), DIGEST))
    return result


def candidates(
    repository: str,
    versions: list[Version],
    cutoff: datetime,
    protected_digests: set[str],
    temporary_tags: re.Pattern[str],
) -> list[Version]:
    marked = {
        v["digest"]
        for v in versions
        if "ci-gc-" + v["digest"].split(":", 1)[1] in v["tags"]
    }
    expired = {v["digest"] for v in versions if v["updated"] < cutoff} | marked
    roots = {
        v["digest"]
        for v in versions
        if v["digest"] in expired
        and v["tags"]
        and all(
            temporary_tags.fullmatch(tag)
            or tag == "ci-gc-" + v["digest"].split(":", 1)[1]
            for tag in v["tags"]
        )
    }
    if not roots:
        return []
    graph: dict[str, set[str]] = {}

    def reachable(seeds: set[str]) -> set[str]:
        seen: set[str] = set()
        pending = list(seeds)
        while pending:
            digest = pending.pop()
            if digest in seen:
                continue
            seen.add(digest)
            if digest not in graph:
                graph[digest] = children(repository, digest)
            pending.extend(graph[digest] - seen)
        return seen

    temporary = reachable(roots)
    eligible = {
        v["digest"]
        for v in versions
        if v["digest"] in temporary & expired
        and (v["digest"] in roots or not v["tags"])
    }
    protected = reachable(
        protected_digests
        | {v["digest"] for v in versions if v["digest"] not in eligible}
    )
    remaining = {
        v["digest"]: v for v in versions if v["digest"] in eligible - protected
    }
    try:
        child_first = tuple(
            TopologicalSorter(
                {
                    digest: graph[digest] & remaining.keys()
                    for digest in remaining
                }
            ).static_order()
        )
    except CycleError as error:
        raise ValueError(
            "The image manifest graph contains a cycle."
        ) from error

    # Retain temporary ancestors until their untagged children expire.
    retained = {
        v["digest"]
        for v in versions
        if not v["tags"] and v["digest"] not in expired
    }
    for digest in child_first:
        if graph[digest] & retained:
            retained.add(digest)
    retained = reachable(retained)
    return [
        remaining[digest]
        for digest in reversed(child_first)
        if digest not in retained
    ]


def cleanup(
    protected: dict[str, set[str]],
    temporary_tags: re.Pattern[str],
    retention_days: int,
    *,
    apply: bool = False,
) -> None:
    if retention_days <= 0:
        raise ValueError("Retention days must be positive.")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    planned = [
        (
            repository,
            candidates(
                repository,
                package_versions(repository),
                cutoff,
                digests,
                temporary_tags,
            ),
        )
        for repository, digests in protected.items()
    ]
    if apply:
        # Mark children before deleting their parent indexes.
        for repository, versions in planned:
            for version in versions:
                if not version["tags"]:
                    reference = f"{repository}@{version['digest']}"
                    marker = (
                        repository
                        + ":ci-gc-"
                        + version["digest"].split(":", 1)[1]
                    )
                    command(
                        "docker",
                        "buildx",
                        "imagetools",
                        "create",
                        "--prefer-index=false",
                        "--tag",
                        marker,
                        reference,
                    )
                    if registry_digest(marker) != version["digest"]:
                        raise ValueError(
                            "The cleanup marker changed the image digest."
                        )
    for repository, versions in planned:
        for version in versions:
            print(
                f"{'Delete' if apply else 'Would delete'} "
                f"{repository}@{version['digest']}",
                flush=True,
            )
            if apply:
                gh(
                    "api",
                    "--method",
                    "DELETE",
                    f"{package_api(repository)}/versions/{version['id']}",
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protected_digests_file", type=Path)
    parser.add_argument("--temporary-tag-pattern", required=True)
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--apply", choices=("true", "false"), default="false")
    args = parser.parse_args()
    cleanup(
        protected_digests(args.protected_digests_file),
        re.compile(args.temporary_tag_pattern),
        args.retention_days,
        apply=args.apply == "true",
    )
