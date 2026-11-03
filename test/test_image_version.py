"""Offline checks for image release version selection."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.image_version import increase_version


class ImageVersionTests(unittest.TestCase):
    def test_release_increments(self):
        for kind, expected in (("NONE", "1.2.9"), ("patch", "1.2.10"),
                               ("minor", "1.3.0"), ("major", "2.0.0")):
            with self.subTest(kind=kind):
                self.assertEqual(increase_version("1.2.9", kind, auto_increase=False), expected)

    def test_auto_increase_carries_patch_and_minor(self):
        for version, kind, expected in (
                ("1.0.8", "patch", "1.0.9"),
                ("1.0.9", "patch", "1.1.0"),
                ("1.9.9", "patch", "2.0.0"),
                ("1.9.5", "minor", "2.0.0"),
                ("1.9.9", "major", "2.0.0"),
                ("1.12.13", "NONE", "1.12.13")):
            with self.subTest(version=version, kind=kind):
                self.assertEqual(increase_version(version, kind), expected)

    def test_cli_auto_increase_can_be_disabled(self):
        script = Path("scripts/image_version.py").resolve()
        with tempfile.TemporaryDirectory(prefix="image-version-test-") as temporary:
            path = Path(temporary) / "versions.yml"
            for enabled, expected in (("true", "2.0.0"), ("false", "1.9.10")):
                path.write_text('{"image_version":"1.9.9"}')
                result = subprocess.run([
                    sys.executable, "-B", str(script), "patch", "--auto-increase", enabled,
                    "--file", str(path)], text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertEqual(result.stderr, f"Current version: 1.9.9\nNew version: {expected}\n")
                self.assertEqual(json.loads(path.read_text())["image_version"], expected)

    def test_invalid_versions_and_increments(self):
        for version in ("1.2", "v1.2.3", "1.2.3-rc1", "01.2.3", "1.2.3\n"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                increase_version(version, "patch")
        with self.assertRaises(ValueError):
            increase_version("1.2.3", "other")

    def test_cli_preserves_pin_and_none_keeps_file_unchanged(self):
        script = Path("scripts/image_version.py").resolve()
        with tempfile.TemporaryDirectory(prefix="image-version-test-") as temporary:
            path = Path(temporary) / "versions.yml"
            original = '{"image_version":"1.2.3","flutter_version":"3.47.5","flutter_base_digest":"checksum"}\n'
            path.write_text(original)
            result = subprocess.check_output([sys.executable, "-B", str(script), "NONE", "--file", str(path)], text=True)
            self.assertEqual(result.strip(), "1.2.3")
            self.assertEqual(path.read_text(), original)
            result = subprocess.check_output([sys.executable, "-B", str(script), "minor", "--file", str(path)], text=True)
            self.assertEqual(result.strip(), "1.3.0")
            self.assertEqual(json.loads(path.read_text()), {
                "image_version": "1.3.0", "flutter_version": "3.47.5", "flutter_base_digest": "checksum"})


if __name__ == "__main__":
    unittest.main()
