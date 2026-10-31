import subprocess
import unittest
from unittest.mock import patch

from prepare_release import reject_existing_versions, release_metadata


class ReleaseServiceTests(unittest.TestCase):
    def metadata(self, ref: str, run_id: str) -> dict[str, str]:
        return release_metadata(ref, run_id)

    def test_dev_versions_are_unique_per_run(self) -> None:
        first = self.metadata("refs/heads/master", "101")
        second = self.metadata("refs/heads/master", "102")
        self.assertNotEqual(first["version"], second["version"])
        self.assertTrue(first["version"].endswith("-dev.101"))
        self.assertEqual(first["prerelease"], "true")

    def test_stable_tag_is_preserved(self) -> None:
        self.assertEqual(
            self.metadata("refs/tags/v26.10.1", "101"),
            {"tag": "v26.10.1", "version": "26.10.1", "prerelease": ""},
        )

    def test_invalid_tag_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.metadata("refs/tags/invalid", "101")

    def guard(self, replies: list[subprocess.CompletedProcess[str]]) -> None:
        with patch(
            "prepare_release.subprocess.run", side_effect=replies
        ) as run:
            reject_existing_versions(
                "26.10.1", "platformpayments", "platform-payments", ""
            )
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["timeout"], 60)

    def test_missing_versions_can_publish(self) -> None:
        self.guard(
            [
                subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    "ERROR: ghcr.io/neuro-inc/"
                    "platformpayments:26.10.1: not found",
                ),
                subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    "Error: ghcr.io/neuro-inc/helm-charts/"
                    "platform-payments:26.10.1: not found",
                ),
            ]
        )

    def test_existing_image_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.guard([subprocess.CompletedProcess([], 0, "{}", "")])

    def test_existing_chart_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.guard(
                [
                    subprocess.CompletedProcess(
                        [],
                        1,
                        "",
                        "ERROR: ghcr.io/neuro-inc/"
                        "platformpayments:26.10.1: not found",
                    ),
                    subprocess.CompletedProcess([], 0, "version: 26.10.1", ""),
                ]
            )

    def test_lookup_errors_fail_closed(self) -> None:
        for error in [
            "unauthorized",
            "connection timed out",
            "forbidden: not found",
        ]:
            with (
                self.subTest(error=error),
                self.assertRaisesRegex(RuntimeError, "Cannot verify"),
            ):
                self.guard([subprocess.CompletedProcess([], 1, "", error)])

    def test_404_in_version_does_not_hide_timeout(self) -> None:
        response = subprocess.CompletedProcess(
            [], 1, "", "timeout fetching example:26.10.0-dev.404123"
        )
        with patch("prepare_release.subprocess.run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                reject_existing_versions(
                    "26.10.0-dev.404123", "example", "", ""
                )

    def test_lookup_timeout_stops_publication(self) -> None:
        with patch(
            "prepare_release.subprocess.run",
            side_effect=subprocess.TimeoutExpired("docker", 60),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                reject_existing_versions("26.10.1", "example", "", "")


if __name__ == "__main__":
    unittest.main()
