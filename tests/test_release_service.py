import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def script(name: str) -> str:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github/workflows/release-service.yaml"
    ).read_text()
    step = workflow.split(f"    - name: {name}\n", 1)[1]
    step = step.split("\n    - ", 1)[0]
    body = step.split("      run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith("        "):
            break
        lines.append(line[8:] if line else "")
    return "\n".join(lines)


class ReleaseServiceTests(unittest.TestCase):
    def metadata(self, ref: str, run_id: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REF": ref,
                    "GITHUB_RUN_ID": run_id,
                    "GITHUB_OUTPUT": str(output),
                },
            ):
                exec(
                    compile(script("Detect metadata"), "<metadata>", "exec"),
                    {},
                )
            return dict(
                line.split("=", 1) for line in output.read_text().splitlines()
            )

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
        with patch.dict(
            os.environ,
            {
                "VERSION": "26.10.1",
                "IMAGE": "platformpayments",
                "CHARTS": "platform-payments",
                "CHART_PREFIX": "",
            },
        ):
            with patch("subprocess.run", side_effect=replies) as run:
                exec(
                    compile(
                        script("Reject existing release versions"),
                        "<guard>",
                        "exec",
                    ),
                    {},
                )
                self.assertEqual(run.call_count, 2)

    def test_missing_versions_can_publish(self) -> None:
        self.guard(
            [
                subprocess.CompletedProcess([], 1, "", "manifest unknown"),
                subprocess.CompletedProcess([], 1, "", "not found"),
            ]
        )

    def test_existing_image_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.guard([subprocess.CompletedProcess([], 0, "{}", "")])

    def test_existing_chart_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.guard(
                [
                    subprocess.CompletedProcess([], 1, "", "manifest unknown"),
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


if __name__ == "__main__":
    unittest.main()
