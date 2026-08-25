"""Persisted runtime projection for an auditable decision policy."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

import yaml


RUNTIME_SCHEMA_VERSION = 1
RUNTIME_TOP_LEVEL_FIELDS = (
    "version",
    "profile_summary",
    "core_principles",
    "current_state_gates",
    "preferences",
    "heuristics",
    "stop_decision",
    "vocabulary",
    "strategic_frame",
    "ai_collaboration_modes",
    "redlines",
)
PROVENANCE_FIELDS = frozenset({"evidence_turn_ids", "derived_from", "derived_from_labels"})


def runtime_policy_path(source_path: Path) -> Path:
    source = Path(source_path).expanduser()
    if source.name.endswith(".runtime.yaml"):
        return source
    if source.suffix != ".yaml":
        raise ValueError(f"decision policy source must be a .yaml file: {source}")
    return source.with_name(f"{source.stem}.runtime.yaml")


def compile_runtime_policy(source_path: Path, *, out_path: Path | None = None) -> Path:
    source = Path(source_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"decision policy source not found: {source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"decision policy must be a mapping: {source}")
    for required in ("core_principles", "preferences", "redlines"):
        if required not in data:
            raise ValueError(f"decision policy missing required section {required!r}: {source}")

    target = Path(out_path).expanduser() if out_path is not None else runtime_policy_path(source)
    if not target.name.endswith(".runtime.yaml"):
        raise ValueError(f"runtime decision policy output must end with .runtime.yaml: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_runtime_policy(source, data)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(rendered)
            temp_path = Path(handle.name)
        temp_path.replace(target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return target


def load_runtime_policy(source_path: Path) -> dict[str, Any]:
    source = _source_policy_path(Path(source_path).expanduser())
    runtime = runtime_policy_path(source)
    if not runtime.is_file():
        raise FileNotFoundError(f"runtime decision policy not found: {runtime}; run `uv run keep-going compile-policy`")
    if not source.is_file():
        raise FileNotFoundError(f"decision policy source not found: {source}")

    expected = _render_runtime_policy(source)
    actual = runtime.read_text(encoding="utf-8")
    if actual != expected:
        raise ValueError(f"runtime decision policy is stale or modified: {runtime}; run `uv run keep-going compile-policy`")
    data = yaml.safe_load(actual)
    if not isinstance(data, dict):
        raise ValueError(f"runtime decision policy must be a mapping: {runtime}")
    return data


def _render_runtime_policy(source: Path, data: dict[str, Any] | None = None) -> str:
    loaded = data if data is not None else yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"decision policy must be a mapping: {source}")
    document: dict[str, Any] = {
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    for field in RUNTIME_TOP_LEVEL_FIELDS:
        if field in loaded:
            document[field] = _strip_provenance(loaded[field])
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def _source_policy_path(path: Path) -> Path:
    suffix = ".runtime.yaml"
    if path.name.endswith(suffix):
        return path.with_name(f"{path.name[:-len(suffix)]}.yaml")
    return path


def _strip_provenance(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_provenance(item) for key, item in value.items() if key not in PROVENANCE_FIELDS}
    if isinstance(value, list):
        return [_strip_provenance(item) for item in value]
    return value
