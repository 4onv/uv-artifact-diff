#!/usr/bin/env python3
"""Review recorded wheel, source archive, and source changes in two uv.lock files."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from urllib.parse import unquote, urlsplit


VERSION = "0.1.0"
MAX_BYTES = 16 * 1024 * 1024
NAME = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9._-]*[a-zA-Z0-9])?")
RELEASE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.!+_-]{0,159}")
FILENAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+!-]{0,254}")
DIGEST = re.compile(r"[a-z0-9_-]{1,20}:[a-f0-9]{16,256}")
SOURCE_KINDS = {"registry", "git", "url", "path", "directory", "editable", "virtual"}


class LockError(ValueError):
    """Input outside the supported portion of the lockfile format."""


@dataclass(frozen=True)
class Artifact:
    kind: str
    filename: str
    locator: tuple[str, str]
    digest: str | None
    size: int | None


@dataclass(frozen=True)
class Variant:
    source: tuple[str, str]
    artifacts: dict[tuple[str, str], Artifact]


def read_lock(path):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise LockError("Expected a regular lockfile of at most 16 MiB.")
        data = handle.read(info.st_size + 1)
        if len(data) != info.st_size:
            raise LockError("Lockfile changed size while reading. Retry with a stable copy.")
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise LockError("Expected valid UTF-8 TOML.") from exc


def nonnegative(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def package_name(value):
    if not isinstance(value, str) or len(value) > 214 or not NAME.fullmatch(value):
        raise LockError("A package has an unsupported name.")
    return re.sub(r"[-_.]+", "-", value).lower()


def source_entry(value):
    if not isinstance(value, dict) or len(value) != 1:
        raise LockError("Expected exactly one source selector per package variant.")
    kind, locator = next(iter(value.items()))
    if kind not in SOURCE_KINDS or not isinstance(locator, str) or not locator or len(locator) > 8192:
        raise LockError("Unsupported source selector in lockfile.")
    return kind, locator


def resolution_markers(value):
    if not isinstance(value, list) or len(value) > 256 or any(
        not isinstance(marker, str) or not marker or len(marker) > 4096 for marker in value
    ):
        raise LockError("Expected a list of package resolution marker strings.")
    return tuple(sorted(set(value)))


def artifact_entry(value, kind):
    if not isinstance(value, dict):
        raise LockError("Expected an artifact table.")
    locations = [key for key in ("url", "path", "filename") if key in value]
    if len(locations) != 1:
        raise LockError("Expected exactly one artifact URL, path or filename.")
    field = locations[0]
    raw = value[field]
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        raise LockError("Unexpected artifact location.")
    if field == "url":
        try:
            filename = unquote(urlsplit(raw).path.rsplit("/", 1)[-1], errors="strict")
        except ValueError as exc:
            raise LockError("Could not read a filename from an artifact URL.") from exc
    elif field == "path":
        filename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    else:
        filename = raw
    if not FILENAME.fullmatch(filename) or (kind == "wheel" and not filename.endswith(".whl")):
        raise LockError("Artifact has an unsupported filename.")
    digest = value.get("hash")
    if digest is not None:
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest.lower()):
            raise LockError("Artifact has an unsupported digest.")
        digest = digest.lower()
        algorithm, hex_value = digest.split(":")
        expected = {"sha256": 64, "sha384": 96, "sha512": 128}.get(algorithm)
        if len(hex_value) % 2 or (expected is not None and len(hex_value) != expected):
            raise LockError("Artifact digest has the wrong length.")
    size = value.get("size")
    if size is not None and not nonnegative(size):
        raise LockError("Artifact size must be a nonnegative integer.")
    return Artifact(kind, filename, (field, raw), digest, size)


def collect_artifacts(package):
    wheels = package.get("wheels", [])
    if not isinstance(wheels, list):
        raise LockError("Expected a list of wheels.")
    entries = [artifact_entry(wheel, "wheel") for wheel in wheels]
    if "sdist" in package:
        entries.append(artifact_entry(package["sdist"], "sdist"))
    result = {(item.kind, item.filename): item for item in entries}
    if len(result) != len(entries):
        raise LockError("Two artifacts share a kind and filename in one variant.")
    return result


def parse_lock(document):
    if type(document.get("version")) is not int or document["version"] != 1:
        raise LockError("Only uv lockfile schema version 1 is supported.")
    revision = document.get("revision")
    if revision is not None and not nonnegative(revision):
        raise LockError("Invalid lockfile revision.")
    packages = document.get("package")
    if not isinstance(packages, list) or not packages:
        raise LockError("Expected a nonempty list of locked packages.")
    groups = {}
    for row in packages:
        if not isinstance(row, dict):
            raise LockError("Expected a package table.")
        name = package_name(row.get("name"))
        version = row.get("version")
        if version is not None and (not isinstance(version, str) or not RELEASE.fullmatch(version)):
            raise LockError("A package has an unsupported version string.")
        source = source_entry(row.get("source"))
        if version is None and source[0] not in {"virtual", "editable", "directory", "path"}:
            raise LockError("A nonlocal package is missing its version.")
        # The empty sentinel sorts before versioned releases and is rendered as null.
        group = groups.setdefault((name, version or ""), {})
        selected = resolution_markers(row.get("resolution-markers", []))
        if selected in group:
            raise LockError("Package variants share the same name, version and resolution markers.")
        group[selected] = Variant(source, collect_artifacts(row))
    return revision, groups


def artifact_label(key):
    return {"kind": key[0], "filename": key[1]}


def compare_variant(before, after):
    changes = {}
    if before.source != after.source:
        changes["source"] = {"beforeKind": before.source[0], "afterKind": after.source[0]}
    old, new = before.artifacts, after.artifacts
    added = [artifact_label(key) for key in sorted(new.keys() - old.keys())]
    removed = [artifact_label(key) for key in sorted(old.keys() - new.keys())]
    changed = []
    for key in sorted(old.keys() & new.keys()):
        fields = []
        a, b = old[key], new[key]
        if a.locator != b.locator:
            fields.append("locator")
        if a.digest != b.digest:
            fields.append("digest")
        if a.size != b.size:
            fields.append("size")
        if fields:
            row = {**artifact_label(key), "fields": fields}
            if "digest" in fields:
                row["digestRecorded"] = {"before": a.digest is not None, "after": b.digest is not None}
            changed.append(row)
    if added or removed or changed:
        changes["artifacts"] = {"added": added, "removed": removed, "changed": changed}
    return changes


def package_label(key):
    return {"name": key[0], "version": key[1] or None}


def compare_locks(before, after):
    before_revision, old = parse_lock(before)
    after_revision, new = parse_lock(after)
    retained_changes = []
    for key in sorted(old.keys() & new.keys()):
        a, b = old[key], new[key]
        shared = a.keys() & b.keys()
        variant_changes = []
        for index, markers in enumerate(sorted(a), 1):
            if markers not in shared:
                continue
            diff = compare_variant(a[markers], b[markers])
            if diff:
                variant_changes.append({"beforeVariant": index, "selectorCount": len(markers), **diff})
        if a.keys() != b.keys() or variant_changes:
            retained_changes.append({
                **package_label(key), "beforeVariants": len(a), "afterVariants": len(b),
                "selectorSetChanged": a.keys() != b.keys(), "sharedVariants": len(shared),
                "variants": variant_changes,
            })
    return {
        "schemaVersion": 1,
        "lockRevisions": {"before": before_revision, "after": after_revision},
        "addedVersions": [package_label(key) for key in sorted(new.keys() - old.keys())],
        "removedVersions": [package_label(key) for key in sorted(old.keys() - new.keys())],
        "retainedVersionChanges": retained_changes,
    }


def demo():
    before = {"version": 1, "revision": 5, "package": [{
        "name": "synthetic-hello", "version": "1.2.0",
        "source": {"registry": "https://packages.example.invalid/simple"},
        "wheels": [
            {"url": "https://files.example.invalid/synthetic_hello-1.2.0-py3-none-any.whl", "hash": "sha256:" + "a" * 64},
            {"url": "https://files.example.invalid/synthetic_hello-1.2.0-cp312-cp312-win_amd64.whl"},
        ],
    }]}
    package = before["package"][0]
    after = {"version": 1, "revision": 5, "package": [{
        **package, "wheels": [
            {**package["wheels"][0], "hash": "sha256:" + "b" * 64},
            {"url": "https://files.example.invalid/synthetic_hello-1.2.0-cp312-cp312-macosx_14_0_arm64.whl"},
        ],
    }]}
    return {"synthetic": True, **compare_locks(before, after)}


def package_title(row):
    return f"{row['name']} {row['version'] or '(unversioned)'}"


def print_report(report):
    if report.get("synthetic"):
        print("SYNTHETIC DEMO - invented lockfile data")
    print(f"New version entries: {len(report['addedVersions'])}; removed: {len(report['removedVersions'])}")
    for category, label in (("addedVersions", "+"), ("removedVersions", "-")):
        for row in report[category]:
            print(f"{label} {package_title(row)}")
    retained = report["retainedVersionChanges"]
    print(f"Retained versions with recorded changes: {len(retained)}")
    for row in retained:
        print(package_title(row))
        if row["selectorSetChanged"]:
            print(f"  Resolution selectors changed ({row['sharedVariants']} shared variants).")
        for variant in row["variants"]:
            print(f"  Baseline variant {variant['beforeVariant']} ({variant['selectorCount']} resolution markers)")
            if "source" in variant:
                source = variant["source"]
                print(f"    Source changed ({source['beforeKind']} -> {source['afterKind']})")
            for action, items in variant.get("artifacts", {}).items():
                for item in items:
                    fields = ", ".join(item.get("fields", []))
                    description = f" ({fields})" if fields else ""
                    print(f"    {action} {item['kind']} {item['filename']}{description}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("diff", help="compare two lockfiles", allow_abbrev=False)
    check.add_argument("before", type=Path)
    check.add_argument("after", type=Path)
    check.add_argument("--fail-on-retained-change", action="store_true")
    check.add_argument("--json", action="store_true")
    example = commands.add_parser("demo", help="run the synthetic example", allow_abbrev=False)
    example.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = demo() if args.command == "demo" else compare_locks(read_lock(args.before), read_lock(args.after))
    except LockError as exc:
        print(f"uv-artifact-diff: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"uv-artifact-diff: Could not read the selected input ({exc.__class__.__name__}).", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return int(getattr(args, "fail_on_retained_change", False) and bool(report["retainedVersionChanges"]))


if __name__ == "__main__":
    raise SystemExit(main())
