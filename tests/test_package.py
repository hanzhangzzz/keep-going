from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.config import (
    Config,
    FiltersCfg,
    ModelsCfg,
    PathsCfg,
    ReasoningCfg,
    ScrubCfg,
    SourcesCfg,
    WindowCfg,
)
from keep_going.integration.package import PLUGIN_PUBLIC_FILES, package_keep_going
from keep_going.decision.policy_runtime import compile_runtime_policy


def _config(root: Path) -> Config:
    return Config(
        window=WindowCfg(days=90),
        sources=SourcesCfg(
            claude_code_dir=root / "claude",
            codex_archived_dir=root / "codex",
            codex_history=root / "history.jsonl",
        ),
        paths=PathsCfg(data_dir=root / "data", artifacts_dir=root / "artifacts"),
        scrub=ScrubCfg(enabled=True, user_replacement="USER"),
        models=ModelsCfg(reasoning="reasoning", eval="eval", decision="keep-going-model"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=root,
    )


def _write_fixture(root: Path) -> Config:
    cfg = _config(root)
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    _write(
        cfg.paths.artifacts_dir / "decision-policy.template.yaml",
        yaml.safe_dump({"version": "template", "core_principles": []}),
    )
    _write(
        source,
        yaml.safe_dump(
            {"version": 0.4, "core_principles": [{"id": "x"}], "preferences": {}, "redlines": []}
        ),
    )
    compile_runtime_policy(source)
    _write(root / ".codex" / "skills" / "keep-going" / "SKILL.md", "skill")
    _write(root / ".codex" / "agents" / "keep-going.toml", 'name = "keep-going"')
    _write(root / ".claude-plugin" / "marketplace.json", json.dumps({"name": "keep-going-local", "plugins": []}))
    _write(root / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json", json.dumps({"name": "keep-going"}))
    _write(root / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json", json.dumps({"name": "keep-going"}))
    _write(root / "plugins" / "keep-going" / "scripts" / "reply.sh", "#!/usr/bin/env bash\n")
    for relative in PLUGIN_PUBLIC_FILES:
        path = root / "plugins" / "keep-going" / relative
        if not path.exists():
            _write(path, "{}" if path.suffix == ".json" else "public\n")
    return cfg


def test_package_keep_going_exports_manifest_and_integration_surfaces(tmp_path: Path):
    cfg = _write_fixture(tmp_path)

    out = package_keep_going(cfg, out_dir=tmp_path / "out" / "keep-going-package")

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "keep-going-me"
    assert manifest["repo_bound"] is False
    assert manifest["policy"] == {"template_path": "artifacts/decision-policy.template.yaml"}
    assert manifest["privacy"]["includes_labeled_turns"] is False
    assert manifest["privacy"]["includes_private_policy"] is False
    assert (out / "artifacts" / "decision-policy.template.yaml").exists()
    assert not (out / "decision-policy.yaml").exists()
    assert not (out / "decision-policy.runtime.yaml").exists()
    assert (out / "skills" / "keep-going" / "SKILL.md").exists()
    assert (out / "agents" / "keep-going.toml").exists()
    assert (out / ".claude-plugin" / "marketplace.json").exists()
    assert (out / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json").exists()
    assert (out / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json").exists()
    assert not (out / "plugins" / "keep-going" / "runtime-root").exists()
    assert not (out / "plugins" / "keep-going" / ".repo-root").exists()
    assert str(cfg.root) not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in out.rglob("*")
        if path.is_file()
    )
    assert manifest["entrypoints"]["bridge"] == "plugins/keep-going/scripts/bridge.sh"
    assert "onboard" not in manifest["entrypoints"]
    assert "not standalone" in manifest["activation"]["onboarding"]
    assert "initialize_policy" in manifest["activation"]


def test_package_keep_going_refuses_existing_target(tmp_path: Path):
    cfg = _write_fixture(tmp_path)
    out = tmp_path / "keep-going-package"
    out.mkdir()

    try:
        package_keep_going(cfg, out_dir=out)
    except FileExistsError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected FileExistsError")


def test_package_keep_going_does_not_read_or_export_private_policy(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    runtime = cfg.paths.artifacts_dir / "decision-policy.runtime.yaml"
    runtime.write_text(runtime.read_text(encoding="utf-8") + "tampered: true\n", encoding="utf-8")

    out = package_keep_going(cfg, out_dir=tmp_path / "out")

    assert not (out / "decision-policy.yaml").exists()
    assert not (out / "decision-policy.runtime.yaml").exists()


def test_package_keep_going_uses_exact_plugin_allowlist(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    plugin = cfg.root / "plugins" / "keep-going"
    _write(plugin / "private-session.jsonl", "private")
    _write(plugin / "private.png", "private")
    (plugin / "linked.md").symlink_to(plugin / "private-session.jsonl")

    out = package_keep_going(cfg, out_dir=tmp_path / "out")

    assert not (out / "plugins" / "keep-going" / "private-session.jsonl").exists()
    assert not (out / "plugins" / "keep-going" / "private.png").exists()
    assert not (out / "plugins" / "keep-going" / "linked.md").exists()


def test_package_keep_going_rejects_sensitive_content_in_allowlisted_file(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    _write(
        cfg.root / ".codex" / "skills" / "keep-going" / "SKILL.md",
        "private home: /" + "Users/alice/work\n",
    )

    try:
        package_keep_going(cfg, out_dir=tmp_path / "out")
    except ValueError as exc:
        assert "absolute user-home path" in str(exc)
    else:
        raise AssertionError("expected sensitive activation content to be rejected")


def test_package_keep_going_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    cfg = _write_fixture(tmp_path)
    prompts = cfg.root / "plugins" / "keep-going" / "prompts"
    external = tmp_path / "external-prompts"
    shutil.copytree(prompts, external)
    shutil.rmtree(prompts)
    prompts.symlink_to(external, target_is_directory=True)

    try:
        package_keep_going(cfg, out_dir=tmp_path / "out")
    except ValueError as exc:
        assert "contains symlink" in str(exc)
    else:
        raise AssertionError("expected symlinked plugin directory to be rejected")


def test_package_cli_writes_package(monkeypatch, tmp_path: Path):
    cfg = _write_fixture(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    out = tmp_path / "package"

    result = CliRunner().invoke(cli.main, ["package", "--out", str(out)])

    assert result.exit_code == 0
    assert "wrote Keep Going package" in result.output
    assert (out / "manifest.json").exists()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
