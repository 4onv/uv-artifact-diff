# uv Artifact Diff

**A dependency can keep the same version while its locked wheels, hashes, or source change.** Compare two `uv.lock` files to see those changes separately from version additions and removals.

The CLI reads local files, needs only Python 3.11+, and reports artifact changes without printing raw URLs, local directory paths, marker expressions or hash values.

## Try it

Download `uv_artifact_diff.py` to run it anywhere. [Standalone release](https://github.com/4onv/uv-artifact-diff/releases/latest)

```sh
python uv_artifact_diff.py demo
```

The visibly synthetic demo keeps one package at version `1.2.0`. It shows a macOS wheel added, a Windows wheel removed, and a new recorded hash for the universal wheel. `demo --json` includes `"synthetic": true`.

## Compare two lockfiles

```sh
git show main:uv.lock > uv.before.lock
python uv_artifact_diff.py diff uv.before.lock uv.lock
python uv_artifact_diff.py diff uv.before.lock uv.lock --json --fail-on-retained-change
```

In Windows PowerShell 5.1, save the baseline using UTF-8 without a BOM, or use Git Bash for the `git show` example.

- **Added and removed version entries** are reported separately. A normal version bump appears as one removal and one addition.
- **Retained versions** flag variant selector changes and compare matched variants by source and artifact metadata.
- A matched wheel or source archive reports URL/path, recorded digest, or recorded size changes. Missing digest and size values are treated as missing evidence. Changing a recorded value to or from missing is still a change.

The command exits 0 on a valid comparison, 1 when `--fail-on-retained-change` is present and a retained-version entry changes, and 2 for invalid, unsupported, or unreadable input. The gate permits ordinary additions and removals. Review the JSON before deciding which retained changes your CI should block.

## How entries are matched

Package names are normalized, and version strings are compared exactly. An unversioned local workspace record can also be compared. Variants are matched by the sorted set of their recorded package-level `resolution-markers`. Marker strings are compared literally. If the selector set changes, the report identifies the change and the number of shared variants. It compares artifact details only for variants that can be matched.

Within a variant, artifacts are keyed by kind and filename. A filename is read from the URL path, local path, or explicit filename. The full recorded locator is compared privately. Directory, query, or source changes can therefore register even when the displayed filename is identical. Upload timestamps, field order, and wheel order do not affect the comparison. File revisions are reported separately.

The tool supports uv lockfile schema version 1 and local UTF-8 files up to 16 MiB. Unsupported source records, duplicate variant identities, or duplicate artifact names within a variant exit 2. Reports still contain package names and filenames. Review those before sharing.

This is a review of recorded data. It does not evaluate environment markers, choose a wheel for a platform, check licensing or vulnerability status, resolve dependencies, or validate downloaded bytes. Pair it with uv's normal lock, install, and integrity workflows. [uv lockfile documentation](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile)

## Tests and evidence

```sh
python -m unittest discover -s tests -v
python tests/check_upstream.py
```

Unit tests require no network. The upstream check downloads two pinned public uv repository snapshots and validates their hashes and expected version changes. No distributions are downloaded or installed. [November 19 research and selection](docs/research-2026-11-19.md)
