# reuse

This repository contains shared GitHub Actions and reusable workflows.

## GHCR image cleanup

The `cleanup-ghcr-images` composite action removes expired temporary image graphs from explicit GHCR organization packages.
The caller owns release policy and generates a JSON file with package names and protected digests:

```json
{
  "ghcr.io/example/backend": ["sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
  "ghcr.io/example/frontend": []
}
```

Every key identifies a cleanup target, including packages with no protected digests.
The action rejects empty mappings, invalid packages, and invalid digest lists.
It does not read application manifests or infer packages from the caller's repository.

Use an Ubuntu runner with GitHub CLI installed.
Grant the token access to every package in the file.
Grant `packages: write` when applying cleanup.
Serialize protection-file generation, cleanup, and publication with the same workflow concurrency group.
Prevent registry changes outside that group while cleanup runs.

```yaml
permissions:
  contents: read
  packages: write
jobs:
  cleanup:
    runs-on: ubuntu-latest
    concurrency:
      group: application-registry
      cancel-in-progress: false
    steps:
    - uses: actions/checkout@v4
    - name: Collect protected image digests
      run: ./collect-protected-digests > "$RUNNER_TEMP/protected-digests.json"
    - uses: neuro-inc/reuse/.github/actions/cleanup-ghcr-images@<commit-sha>
      with:
        protected-digests-file: ${{ runner.temp }}/protected-digests.json
        temporary-tag-pattern: 'ci-[0-9a-f]{40}'
        retention-days: '90'
        apply: 'false'
```

Pin the action to a reviewed commit SHA.
The optional `token` input defaults to `github.token`.
The action performs a dry run unless `apply` is exactly `true`.
Review the reported candidates before enabling deletion.

The tag pattern must fully match temporary tags.
Any other tag protects that image and its descendants.
Explicit protected digests also protect their descendants.
Recent versions and unknown untagged versions remain protected.
An expired temporary graph retains recent untagged children until those children expire.
Cleanup marks orphaned children with `ci-gc-<digest>` before deleting their parents.
These markers permit safe retries after partial deletion.
Keep the marker prefix reserved for this action.

The action plans all packages before any registry change.
Invalid metadata, registry failures, or marker verification failures stop cleanup before deletion begins.
Deletion failures can leave a partially removed graph, which a later run can finish.

Run the safety tests and type checks with Python 3.12:

```sh
PYTHONPATH=.github/actions/cleanup-ghcr-images python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m pip install mypy==1.18.2
python3 -m mypy
```
