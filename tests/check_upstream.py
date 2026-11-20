"""Validate the CLI on two pinned public uv repository snapshots."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
LOCKS = [
    ("6b2d39dcc390293d51c9c0d2bbcd650f4bcbfdce", "80273e492ea5994319dbe9afcc3f2ebae5ecf79266175e060285ad938a8206df"),
    ("c99faeeeeb8318d874007b4c48635d009838c1a0", "4f67a83787f3b990d735fb734b4af1e99a1c659bec045a0b9077a7e88a21a101"),
]


def main():
    with tempfile.TemporaryDirectory(prefix="uv-artifact-diff-") as directory:
        inputs = []
        for revision, digest in LOCKS:
            url = f"https://raw.githubusercontent.com/astral-sh/uv/{revision}/uv.lock"
            with urllib.request.urlopen(url, timeout=20) as response:
                content = response.read(2 * 1024 * 1024 + 1)
            assert hashlib.sha256(content).hexdigest() == digest, "Unexpected upstream fixture bytes."
            path = Path(directory) / (revision + ".lock")
            path.write_bytes(content)
            inputs.append(str(path))
        output = subprocess.check_output(
            [sys.executable, "-I", str(ROOT / "uv_artifact_diff.py"), "diff", *inputs,
             "--json", "--fail-on-retained-change"],
            timeout=30,
        )
        result = json.loads(output)
        assert result["addedVersions"] == [{"name": "ty", "version": "0.0.106"}]
        assert result["removedVersions"] == [{"name": "ty", "version": "0.0.69"}]
        assert result["retainedVersionChanges"] == []
        print("Pinned upstream uv.lock snapshots matched their digests and expected version change.")


if __name__ == "__main__":
    main()
