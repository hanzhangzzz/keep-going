"""Export a reviewable Keep Going activation package."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from keep_going.config import Config
from keep_going.privacy import content_violations, path_violations


PLUGIN_PUBLIC_FILES = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".mcp.json",
    "commands/self-test.md",
    "commands/setup.md",
    "commands/status.md",
    "hooks.json",
    "hooks/keep-going-stop-hook.sh",
    "hooks/hooks.json",
    "hooks/keep-going-decision-hook.sh",
    "plugin.json",
    "prompts/keep-going-self-test.md",
    "prompts/keep-going-setup.md",
    "prompts/keep-going-status.md",
    "prompts/keep-going:self-test.md",
    "prompts/keep-going:setup.md",
    "prompts/keep-going:status.md",
    "prompts/keep-going.md",
    "scripts/bridge.sh",
    "scripts/mcp.sh",
    "scripts/reply.sh",
    "skills/keep-going/SKILL.md",
)


def package_keep_going(cfg: Config, *, out_dir: Path | None = None, include_agents: bool = False, project: str | None = None) -> Path:
    if include_agents:
        raise ValueError("private agent snapshots cannot be exported in an activation package")
    out = out_dir or cfg.paths.artifacts_dir / "keep-going-package"
    if out.exists():
        raise FileExistsError(f"package target already exists: {out}")
    out.mkdir(parents=True)
    _copy_file(
        cfg.paths.artifacts_dir / "decision-policy.template.yaml",
        out / "artifacts" / "decision-policy.template.yaml",
        source_root=cfg.root,
    )
    _copy_file(
        cfg.root / ".codex" / "skills" / "keep-going" / "SKILL.md",
        out / "skills" / "keep-going" / "SKILL.md",
        source_root=cfg.root,
    )
    _copy_file(
        cfg.root / ".codex" / "agents" / "keep-going.toml",
        out / "agents" / "keep-going.toml",
        source_root=cfg.root,
    )
    _copy_file(
        cfg.root / ".claude-plugin" / "marketplace.json",
        out / ".claude-plugin" / "marketplace.json",
        source_root=cfg.root,
    )
    _copy_plugin(cfg.root / "plugins" / "keep-going", out / "plugins" / "keep-going")

    manifest = _manifest(out)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _assert_public_package(out)
    return out


def _manifest(out: Path) -> dict[str, object]:
    contents = [
        "manifest.json",
        "artifacts/decision-policy.template.yaml",
        "skills/keep-going/SKILL.md",
        "agents/keep-going.toml",
        "plugins/keep-going/",
        ".claude-plugin/marketplace.json",
    ]
    return {
        "name": "keep-going-me",
        "version": "0.1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_repo": None,
        "repo_bound": False,
        "policy": {"template_path": "artifacts/decision-policy.template.yaml"},
        "entrypoints": {
            "reply": "plugins/keep-going/scripts/reply.sh",
            "mcp": "plugins/keep-going/scripts/mcp.sh",
            "bridge": "plugins/keep-going/scripts/bridge.sh",
        },
        "package_contents": contents,
        "privacy": {
            "includes_labeled_turns": False,
            "includes_raw_logs": False,
            "includes_private_policy": False,
            "includes_agent_policy": False,
            "includes_absolute_paths": False,
            "note": "Private decision policy stays local; initialize it from the template after installation.",
        },
        "activation": {
            "initialize_policy": "copy artifacts/decision-policy.template.yaml to the local ignored decision-policy.yaml, then run keep-going compile-policy",
            "readiness_check": "uv run keep-going audit --smoke --json-output",
        },
    }


def _copy_file(src: Path, dst: Path, *, source_root: Path) -> None:
    _assert_contained_regular_source(src, source_root)
    if not src.is_file():
        raise FileNotFoundError(f"missing package source: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_plugin(src: Path, dst: Path) -> None:
    if src.is_symlink() or not src.is_dir():
        raise ValueError(f"plugin package source must be a regular directory: {src}")
    for relative in PLUGIN_PUBLIC_FILES:
        _copy_file(src / relative, dst / relative, source_root=src)


def _assert_contained_regular_source(src: Path, source_root: Path) -> None:
    try:
        relative = src.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(f"package source escapes approved root: {src}") from exc
    current = source_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"package source path contains symlink: {current}")
    try:
        src.resolve(strict=True).relative_to(source_root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"package source escapes approved root: {src}") from exc


def _assert_public_package(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"activation package contains symlink: {path.relative_to(root)}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        violations = [*path_violations(relative), *content_violations(path.read_bytes())]
        if violations:
            raise ValueError(f"activation package privacy violation: {relative}: {', '.join(violations)}")
