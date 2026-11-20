import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import uv_artifact_diff as artifact_diff


SCRIPT = Path(__file__).resolve().parents[1] / "uv_artifact_diff.py"
DIGEST = "sha256:" + "a" * 64
WHEEL = "sample-1.0-py3-none-any.whl"


def package(**fields):
    return {
        "name": "Sample", "version": "1.0",
        "source": {"registry": "https://registry.example.invalid/simple"},
        "wheels": [{"url": f"https://files.example.invalid/a/{WHEEL}", "size": 42, "hash": DIGEST}],
        **fields,
    }


def lock(*rows, revision=5):
    return {"version": 1, "revision": revision, "package": list(rows)}


class DiffTests(unittest.TestCase):
    def test_ignores_revision_upload_time_and_artifact_order(self):
        original = package()
        original["wheels"].append({"filename": "sample-1.0-cp313-cp313-win_amd64.whl"})
        newer = copy.deepcopy(original)
        newer["wheels"][0]["upload-time"] = "2026-11-19T12:00:00Z"
        newer["wheels"].reverse()
        diff = artifact_diff.compare_locks(lock(original, revision=2), lock(newer))
        self.assertEqual(diff["retainedVersionChanges"], [])
        self.assertEqual(diff["lockRevisions"], {"before": 2, "after": 5})

    def test_reports_locator_hash_and_size_without_sharing_private_values(self):
        newer = package(source={"registry": "https://private-user:secret@private.example.invalid/simple"})
        newer["wheels"][0] = {
            "url": f"https://private.example.invalid/{WHEEL}?secret=secret",
            "hash": "sha256:" + "b" * 64, "size": 43,
        }
        result = artifact_diff.compare_locks(lock(package()), lock(newer))
        [entry] = result["retainedVersionChanges"]
        [variant] = entry["variants"]
        self.assertEqual(variant["source"], {"beforeKind": "registry", "afterKind": "registry"})
        [change] = variant["artifacts"]["changed"]
        self.assertEqual(change["fields"], ["locator", "digest", "size"])
        self.assertEqual(change["digestRecorded"], {"before": True, "after": True})
        output = json.dumps(result)
        self.assertNotIn("secret", output)
        self.assertNotIn("private.example", output)
        self.assertNotIn("sha256:", output)

    def test_source_archive_records_locator_change_and_new_digest(self):
        original = package(sdist={"path": "./tmp/sample-1.0.tar.gz"})
        newer = package(sdist={"path": "./elsewhere/sample-1.0.tar.gz", "hash": DIGEST})
        [changed] = artifact_diff.compare_locks(lock(original), lock(newer))["retainedVersionChanges"]
        [variant] = changed["variants"]
        self.assertEqual(variant["artifacts"]["changed"], [{
            "filename": "sample-1.0.tar.gz", "kind": "sdist", "fields": ["locator", "digest"],
            "digestRecorded": {"before": False, "after": True},
        }])

    def test_new_version_is_separate_from_retained_change(self):
        result = artifact_diff.compare_locks(lock(package()), lock(package(version="2.0")))
        self.assertEqual(result["addedVersions"], [{"name": "sample", "version": "2.0"}])
        self.assertEqual(result["removedVersions"], [{"name": "sample", "version": "1.0"}])
        self.assertEqual(result["retainedVersionChanges"], [])

    def test_keeps_conditional_variants_separate_and_flags_selector_change(self):
        arm = package(**{"resolution-markers": ["platform_machine == 'arm64'"]})
        x86 = package(**{"resolution-markers": ["platform_machine == 'x86_64'"]})
        newer = copy.deepcopy(arm)
        newer["wheels"].clear()
        [entry] = artifact_diff.compare_locks(lock(x86, arm), lock(newer))["retainedVersionChanges"]
        self.assertTrue(entry["selectorSetChanged"])
        self.assertEqual((entry["beforeVariants"], entry["afterVariants"], entry["sharedVariants"]), (2, 1, 1))
        self.assertEqual(entry["variants"][0]["artifacts"]["removed"], [{"kind": "wheel", "filename": WHEEL}])

    def test_unversioned_workspace_entry_can_be_compared(self):
        old = package(version=None, source={"editable": "."}, wheels=[])
        new = package(version=None, source={"editable": "workspace/client"}, wheels=[])
        [entry] = artifact_diff.compare_locks(lock(old), lock(new))["retainedVersionChanges"]
        self.assertIsNone(entry["version"])
        self.assertNotIn("workspace/client", json.dumps(entry))

    def test_rejects_ambiguous_variants_and_duplicate_wheel_filenames(self):
        with self.assertRaisesRegex(artifact_diff.LockError, "same name"):
            artifact_diff.parse_lock(lock(package(), package(source={"registry": "https://second.example.invalid"})))
        duplicate = package()
        duplicate["wheels"].append({"url": f"https://elsewhere.example.invalid/{WHEEL}"})
        with self.assertRaisesRegex(artifact_diff.LockError, "share a kind"):
            artifact_diff.parse_lock(lock(duplicate))

    def test_schema_and_malformed_records_fail_clearly(self):
        for document in (
            {**lock(package()), "version": True},
            {**lock(package()), "version": 2},
            lock(package(wheels=[{"filename": WHEEL, "hash": "sha256:" + "a" * 62}])),
            lock(package(wheels=[{"filename": WHEEL, "size": False}])),
            lock(package(source={"other": "local"})),
        ):
            with self.subTest(document=document), self.assertRaises(artifact_diff.LockError):
                artifact_diff.parse_lock(document)

    def test_demo_labels_data_and_reports_each_inventory_change(self):
        result = artifact_diff.demo()
        self.assertTrue(result["synthetic"])
        [row] = result["retainedVersionChanges"]
        [variant] = row["variants"]
        self.assertEqual({key: len(value) for key, value in variant["artifacts"].items()},
                         {"added": 1, "removed": 1, "changed": 1})


class CliTests(unittest.TestCase):
    def test_cli_gates_retained_changes_and_rejects_unreadable_input(self):
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before lock"
            after = Path(directory) / "after lock"
            original = (
                'version = 1\nrevision = 5\n[[package]]\nname = "sample"\nversion = "1.0"\n'
                'source = { registry = "https://registry.example.invalid" }\n'
                f'wheels = [{{ filename = "{WHEEL}", hash = "{DIGEST}" }}]\n'
            )
            before.write_text(original, encoding="utf-8")
            after.write_text(original.replace("a" * 64, "b" * 64), encoding="utf-8")

            def run(a, b, *flags):
                return subprocess.run(
                    [sys.executable, str(SCRIPT), "diff", str(a), str(b), "--json", *flags],
                    text=True, capture_output=True, timeout=10,
                )

            first = run(before, after)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(len(json.loads(first.stdout)["retainedVersionChanges"]), 1)
            self.assertEqual(run(before, after, "--fail-on-retained-change").returncode, 1)
            self.assertEqual(run(before, before, "--fail-on-retained-change").returncode, 0)
            after.write_bytes(b"\xff")
            invalid = run(before, after)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(invalid.stdout, "")
            self.assertEqual(run(before, Path(directory)).returncode, 2)

    def test_input_size_is_bounded_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.lock"
            path.write_bytes(b"x" * 42)
            with patch.object(artifact_diff, "MAX_BYTES", 32), self.assertRaises(artifact_diff.LockError):
                artifact_diff.read_lock(path)


if __name__ == "__main__":
    unittest.main()
