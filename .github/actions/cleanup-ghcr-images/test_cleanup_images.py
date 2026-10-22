"""Test GHCR cleanup without changing registry data."""

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cleanup_images import (
    Version,
    candidates,
    children,
    cleanup,
    package_versions,
    protected_digests,
    registry_digest,
)


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        self.graph: dict[str, set[str]] = {}
        self.protected: dict[str, set[str]] = {"ghcr.io/org/package": set()}
        mocked = patch(
            "cleanup_images.children",
            side_effect=lambda repository, digest: self.graph.get(
                digest, set()
            ),
        )
        mocked.start()
        self.addCleanup(mocked.stop)

    def version(
        self, number: int, tags: list[str], *, recent: bool = False
    ) -> Version:
        return {
            "id": number,
            "digest": "sha256:" + f"{number:064x}",
            "tags": tags,
            "updated": self.cutoff + timedelta(days=1 if recent else -1),
        }

    def selected(
        self, versions: list[Version], published: set[str] | None = None
    ) -> list[int]:
        return [
            v["id"]
            for v in candidates(
                "repository",
                versions,
                self.cutoff,
                published or set(),
                re.compile(r"ci-[0-9a-f]{40}"),
            )
        ]

    def test_removes_only_expired_ci_versions_and_their_orphan_children(
        self,
    ) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        child = self.version(2, [])
        self.graph[root["digest"]] = {child["digest"]}
        self.assertEqual(
            self.selected([root, child, self.version(3, [])]), [1, 2]
        )

    def test_partial_cleanup_can_resume_without_the_original_index(
        self,
    ) -> None:
        child = self.version(2, [], recent=True)
        child["tags"] = ["ci-gc-" + child["digest"].split(":", 1)[1]]
        self.assertEqual(self.selected([child]), [2])
        child["tags"].append("v1.0")
        self.assertEqual(self.selected([child]), [])

    def test_cleanup_deletes_nested_indexes_before_their_children(
        self,
    ) -> None:
        root = self.version(5, ["ci-" + "a" * 40])
        index = self.version(3, [])
        child = self.version(1, [])
        self.graph = {
            root["digest"]: {index["digest"]},
            index["digest"]: {child["digest"]},
        }
        self.assertEqual(self.selected([child, index, root]), [5, 3, 1])

    def test_manifest_cycle_aborts_before_deletion(self) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        child = self.version(2, [])
        self.graph = {
            root["digest"]: {child["digest"]},
            child["digest"]: {root["digest"]},
        }
        with self.assertRaisesRegex(
            ValueError, "manifest graph contains a cycle"
        ):
            self.selected([root, child])

    def test_preserves_version_master_branch_unknown_and_recent_tags(
        self,
    ) -> None:
        for tag in ["v1.0", "master", "feature-test", "unknown", "a" * 40]:
            with self.subTest(tag=tag):
                self.assertEqual(self.selected([self.version(1, [tag])]), [])
                self.assertEqual(
                    self.selected([self.version(1, ["ci-" + "a" * 40, tag])]),
                    [],
                )
        self.assertEqual(
            self.selected([self.version(1, ["ci-" + "a" * 40], recent=True)]),
            [],
        )

    def test_preserves_shared_children_of_published_nested_indexes(
        self,
    ) -> None:
        expired = self.version(1, ["ci-" + "a" * 40])
        published = self.version(2, ["v1.0"])
        index = self.version(3, [])
        child = self.version(4, [])
        self.graph = {
            expired["digest"]: {child["digest"]},
            published["digest"]: {index["digest"]},
            index["digest"]: {child["digest"]},
        }
        self.assertEqual(
            self.selected([expired, published, index, child]), [1]
        )

    def test_explicit_protected_digests_preserve_their_children(self) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        child = self.version(2, [])
        self.graph[root["digest"]] = {child["digest"]}
        self.assertEqual(self.selected([root, child], {root["digest"]}), [])

    def test_recent_untagged_child_remains(self) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        child = self.version(2, [], recent=True)
        self.graph[root["digest"]] = {child["digest"]}
        self.assertEqual(self.selected([root, child]), [])

    def test_retained_ci_graph_is_removed_after_its_last_child_expires(
        self,
    ) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        index = self.version(2, [])
        child = self.version(3, [], recent=True)
        expired_child = self.version(4, [])
        independent = self.version(5, ["ci-" + "b" * 40])
        unknown = self.version(6, [])
        self.graph = {
            root["digest"]: {index["digest"]},
            index["digest"]: {child["digest"], expired_child["digest"]},
            child["digest"]: set(),
            expired_child["digest"]: set(),
            independent["digest"]: set(),
            unknown["digest"]: set(),
        }
        versions = [root, index, child, expired_child, independent, unknown]

        with patch(
            "cleanup_images.children",
            side_effect=lambda repository, digest: self.graph[digest],
        ):
            first_pass = self.selected(versions)
            self.assertEqual(first_pass, [5])
            for version in versions:
                if version["id"] in first_pass:
                    del self.graph[version["digest"]]
            versions = [
                version
                for version in versions
                if version["id"] not in first_pass
            ]
            self.cutoff += timedelta(days=2)

            second_pass = self.selected(versions)
            self.assertEqual(second_pass[:2], [1, 2])
            self.assertCountEqual(second_pass[2:], [3, 4])

    def test_apply_marks_descendants_before_deleting_parents(self) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        child = self.version(2, [])
        self.graph[root["digest"]] = {child["digest"]}
        events: list[str] = []

        def mark(*args: str) -> str:
            events.append("mark")
            return ""

        def delete(*args: str) -> str:
            events.append(args[-1])
            return ""

        with (
            patch(
                "cleanup_images.package_versions", return_value=[root, child]
            ),
            patch(
                "cleanup_images.command",
                side_effect=mark,
            ),
            patch(
                "cleanup_images.registry_digest", return_value=child["digest"]
            ),
            patch(
                "cleanup_images.gh",
                side_effect=delete,
            ),
        ):
            cleanup(
                self.protected, re.compile(r"ci-[0-9a-f]{40}"), 90, apply=True
            )
        self.assertEqual(
            events,
            [
                "mark",
                "orgs/org/packages/container/package/versions/1",
                "orgs/org/packages/container/package/versions/2",
            ],
        )

    def test_marker_failure_prevents_all_deletions(self) -> None:
        root = self.version(1, ["ci-" + "a" * 40])
        child = self.version(2, [])
        self.graph[root["digest"]] = {child["digest"]}
        with (
            patch(
                "cleanup_images.package_versions", return_value=[root, child]
            ),
            patch("cleanup_images.command", return_value=""),
            patch(
                "cleanup_images.registry_digest",
                return_value="sha256:" + "f" * 64,
            ),
            patch("cleanup_images.gh") as api,
            self.assertRaisesRegex(ValueError, "marker"),
        ):
            cleanup(
                self.protected, re.compile(r"ci-[0-9a-f]{40}"), 90, apply=True
            )
        api.assert_not_called()

    def test_registry_failure_aborts_before_any_deletion(self) -> None:
        with (
            patch(
                "cleanup_images.package_versions",
                side_effect=RuntimeError("unavailable"),
            ),
            patch("cleanup_images.gh") as api,
            self.assertRaises(RuntimeError),
        ):
            cleanup(
                self.protected, re.compile(r"ci-[0-9a-f]{40}"), 90, apply=True
            )
        api.assert_not_called()

    def test_dry_run_never_deletes(self) -> None:
        with (
            patch(
                "cleanup_images.package_versions",
                return_value=[self.version(1, ["ci-" + "a" * 40])],
            ),
            patch("cleanup_images.gh") as api,
        ):
            cleanup(self.protected, re.compile(r"ci-[0-9a-f]{40}"), 90)
        api.assert_not_called()

    def test_package_pages_reject_invalid_metadata(self) -> None:
        for value in [{}, [{}], [[{"id": 1}]]]:
            with (
                patch("cleanup_images.gh", return_value=json.dumps(value)),
                self.assertRaises(ValueError),
            ):
                package_versions("ghcr.io/org/package")

    def test_invalid_manifest_aborts_cleanup(self) -> None:
        for value in [{}, {"schemaVersion": 2, "manifests": [{}]}]:
            with (
                patch(
                    "cleanup_images.command", return_value=json.dumps(value)
                ),
                self.assertRaises(ValueError),
            ):
                children("repository", "sha256:" + "a" * 64)

    def test_later_package_failure_prevents_earlier_package_deletion(
        self,
    ) -> None:
        self.protected = {
            "ghcr.io/org/first": set(),
            "ghcr.io/org/second": set(),
        }
        with (
            patch(
                "cleanup_images.package_versions",
                side_effect=[
                    [self.version(1, ["ci-" + "a" * 40])],
                    RuntimeError("unavailable"),
                ],
            ),
            patch("cleanup_images.gh") as api,
            self.assertRaises(RuntimeError),
        ):
            cleanup(
                self.protected, re.compile(r"ci-[0-9a-f]{40}"), 90, apply=True
            )
        api.assert_not_called()

    def test_caller_selects_temporary_tags(self) -> None:
        versions = [
            self.version(1, ["build-123"]),
            self.version(2, ["ci-" + "a" * 40]),
        ]
        selected = candidates(
            "repository",
            versions,
            self.cutoff,
            set(),
            re.compile(r"build-[0-9]+"),
        )
        self.assertEqual([v["id"] for v in selected], [1])

    def test_retention_days_control_expiry(self) -> None:
        with (
            patch(
                "cleanup_images.package_versions",
                return_value=[self.version(1, ["ci-" + "a" * 40])],
            ),
            patch("cleanup_images.command") as command,
            patch("cleanup_images.gh") as api,
        ):
            cleanup(
                self.protected, re.compile(r"ci-[0-9a-f]{40}"), 100, apply=True
            )
        command.assert_not_called()
        api.assert_not_called()

    def test_invalid_retention_aborts_before_registry_access(self) -> None:
        for days in (0, -1):
            with (
                patch("cleanup_images.package_versions") as versions,
                self.assertRaises(ValueError),
            ):
                cleanup(self.protected, re.compile("ci-.*"), days, apply=True)
            versions.assert_not_called()

    def test_package_inventory_uses_explicit_organization_and_nested_package(
        self,
    ) -> None:
        item = {
            "id": 1,
            "name": "sha256:" + "a" * 64,
            "metadata": {"container": {"tags": ["v1.0"]}},
            "updated_at": "2026-01-01T00:00:00Z",
        }
        with patch(
            "cleanup_images.gh", return_value=json.dumps([[item], []])
        ) as api:
            versions = package_versions("ghcr.io/another-org/nested/package")
        self.assertEqual(versions[0]["tags"], ["v1.0"])
        api.assert_called_once_with(
            "api",
            "orgs/another-org/packages/container/"
            "nested%2Fpackage/versions?per_page=100",
            "--paginate",
            "--slurp",
        )

    def test_marker_digest_requires_valid_registry_metadata(self) -> None:
        values: list[object] = [[], {}, {"digest": "invalid"}]
        for value in values:
            with (
                patch(
                    "cleanup_images.command", return_value=json.dumps(value)
                ),
                self.assertRaises(ValueError),
            ):
                registry_digest("ghcr.io/org/package:marker")
        digest = "sha256:" + "a" * 64
        with patch(
            "cleanup_images.command",
            return_value=json.dumps({"digest": digest}),
        ):
            self.assertEqual(
                registry_digest("ghcr.io/org/package:marker"), digest
            )


class ProtectedDigestsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "protected.json"

    def test_file_explicitly_names_packages_and_optional_protected_digests(
        self,
    ) -> None:
        digest = "sha256:" + "a" * 64
        self.path.write_text(
            json.dumps(
                {"ghcr.io/org/first": [digest], "ghcr.io/org/second": []}
            )
        )
        self.assertEqual(
            protected_digests(self.path),
            {
                "ghcr.io/org/first": {digest},
                "ghcr.io/org/second": set(),
            },
        )

    def test_invalid_protection_data_is_rejected(self) -> None:
        values: list[object] = [
            [],
            {},
            {"ghcr.io/org/pkg": None},
            {"ghcr.io/org/pkg": ["invalid"]},
            {"docker.io/org/pkg": []},
            {"ghcr.io/org/pkg:latest": []},
            {"ghcr.io/org/pkg": [False]},
        ]
        for value in values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    protected_digests(self.path)

    def test_missing_or_malformed_protection_file_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            protected_digests(self.path)
        self.path.write_text("invalid")
        with self.assertRaises(ValueError):
            protected_digests(self.path)

    def test_duplicate_package_cannot_replace_protected_digests(self) -> None:
        self.path.write_text(
            '{"ghcr.io/org/package":["sha256:'
            + "a" * 64
            + '"],"ghcr.io/org/package":[]}'
        )
        with self.assertRaisesRegex(ValueError, "must be unique"):
            protected_digests(self.path)


if __name__ == "__main__":
    unittest.main()
