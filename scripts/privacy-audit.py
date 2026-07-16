#!/usr/bin/env python3
"""Fail when Git-tracked content crosses Keep Going's public privacy boundary."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from keep_going.privacy import content_violations as _content_violations  # noqa: E402
from keep_going.privacy import path_violations as _path_violations  # noqa: E402
from keep_going.privacy import reviewed_media_violations as _reviewed_media_violations  # noqa: E402

ROOT = Path(os.environ.get("KEEP_GOING_PRIVACY_ROOT", SOURCE_ROOT)).resolve()


def _git(*args: str, text: bool = False) -> bytes | str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=text)


def _index_entries() -> list[tuple[str, str]]:
    raw = _git("ls-files", "--stage", "-z")
    assert isinstance(raw, bytes)
    entries: list[tuple[str, str]] = []
    for value in raw.split(b"\0"):
        if not value:
            continue
        metadata, raw_path = value.split(b"\t", 1)
        _, object_id, stage = metadata.split(b" ", 2)
        if stage != b"0":
            raise RuntimeError("privacy audit requires an index without unresolved merge entries")
        entries.append((object_id.decode("ascii"), raw_path.decode("utf-8", errors="surrogateescape")))
    return entries


def _tree_entries(treeish: str) -> list[tuple[str, str]]:
    raw = _git("ls-tree", "-r", "-z", treeish)
    assert isinstance(raw, bytes)
    entries: list[tuple[str, str]] = []
    for value in raw.split(b"\0"):
        if not value:
            continue
        metadata, raw_path = value.split(b"\t", 1)
        _, object_type, object_id = metadata.split(b" ", 2)
        if object_type == b"blob":
            entries.append((object_id.decode("ascii"), raw_path.decode("utf-8", errors="surrogateescape")))
    return entries


def _audit_entries(entries: list[tuple[str, str]], scope: str) -> list[str]:
    violations: list[str] = []
    blob_cache: dict[str, bytes] = {}
    for object_id, path in entries:
        for reason in _path_violations(path):
            violations.append(f"{scope}:{path}: {reason}")
        data = blob_cache.get(object_id)
        if data is None:
            raw = _git("cat-file", "blob", object_id)
            assert isinstance(raw, bytes)
            data = raw
            blob_cache[object_id] = data
        for reason in _content_violations(data):
            violations.append(f"{scope}:{path}: {reason}")
        for reason in _reviewed_media_violations(path, data):
            violations.append(f"{scope}:{path}: {reason}")
    return violations


def audit_tracked_tree(treeish: str | None = None) -> list[str]:
    entries = _tree_entries(treeish) if treeish else _index_entries()
    return _audit_entries(entries, f"tree:{treeish}" if treeish else "index")


def audit_history() -> list[str]:
    raw = _git("rev-list", "--all")
    assert isinstance(raw, bytes)
    violations: list[str] = []
    for raw_commit in raw.splitlines():
        commit = raw_commit.decode("ascii")
        violations.extend(_audit_entries(_tree_entries(commit), f"history:{commit[:12]}"))
        message = _git("show", "-s", "--format=%B", commit)
        assert isinstance(message, bytes)
        for reason in _content_violations(message):
            violations.append(f"history:{commit[:12]}: commit message: {reason}")
    metadata = _git("log", "--all", "--format=%H%x09%ae%x09%ce")
    assert isinstance(metadata, bytes)
    for line in metadata.splitlines():
        commit, author, committer = line.split(b"\t", 2)
        for role, email in (("author", author), ("committer", committer)):
            if email and not email.endswith(b"@users.noreply.github.com"):
                violations.append(f"history:{commit.decode('ascii')[:12]}: private {role} email")
    tags = _git("for-each-ref", "--format=%(objecttype)%09%(objectname)", "refs/tags")
    assert isinstance(tags, bytes)
    for line in tags.splitlines():
        object_type, object_id = line.split(b"\t", 1)
        if object_type != b"tag":
            continue
        tag = _git("cat-file", "tag", object_id.decode("ascii"))
        assert isinstance(tag, bytes)
        for reason in _content_violations(tag):
            violations.append(f"history:tag:{object_id.decode('ascii')[:12]}: {reason}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="also scan every object reachable from local refs")
    parser.add_argument("--treeish", help="scan a commit/tree instead of the current Git index")
    args = parser.parse_args()
    violations = audit_tracked_tree(args.treeish)
    if args.history:
        violations.extend(audit_history())
    if violations:
        print("privacy audit: FAIL")
        for violation in sorted(set(violations)):
            print(f"- {violation}")
        return 1
    print("privacy audit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
