"""U5 tests: Codex agent TOML registration and npm wrapper --agent/--agents/--render-mode passthrough."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.agents.registry import load_meta, register_codex_agent, save_meta
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

ROOT = Path(__file__).resolve().parents[1]


# ── helpers ──────────────────────────────────────────────────────────────────

def _config(root: Path, *, artifacts_dir: Path | None = None) -> Config:
    return Config(
        window=WindowCfg(days=90),
        sources=SourcesCfg(
            claude_code_dir=root / "claude",
            codex_archived_dir=root / "codex",
            codex_history=root / "history.jsonl",
        ),
        paths=PathsCfg(data_dir=root / "data", artifacts_dir=artifacts_dir or root / "artifacts"),
        scrub=ScrubCfg(enabled=True, user_replacement="USER"),
        models=ModelsCfg(reasoning="r", eval="e", decision="t"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=root,
    )


def _write_template_policy(artifacts_dir: Path) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    policy = artifacts_dir / "decision-policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "version": 0.4,
                "core_principles": [{"id": "p1", "statement": "test"}],
                "preferences": [],
                "redlines": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return policy


def _scaffold_agent(agent_home: Path, name: str, policy_content: str = "") -> Path:
    agent_dir = agent_home / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    policy_path = agent_dir / "policy-test.yaml"
    policy_path.write_text(policy_content or yaml.safe_dump(
        {"version": 0.1, "core_principles": [{"id": "p1", "statement": "test"}], "preferences": [], "redlines": []},
        allow_unicode=True,
    ), encoding="utf-8")
    save_meta(agent_dir, {
        "name": name,
        "description": f"test agent {name}",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "current_policy": str(policy_path),
        "history": [],
    })
    return policy_path


# ── register_codex_agent ─────────────────────────────────────────────────────

def test_register_creates_toml_file(tmp_path: Path):
    codex_home = tmp_path / "codex"
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 0.1\n", encoding="utf-8")

    result = register_codex_agent("qa-reviewer", policy_path, codex_home=codex_home)
    assert result == codex_home / "agents" / "qa-reviewer.toml"
    assert result.exists()
    content = result.read_text(encoding="utf-8")
    assert "qa-reviewer" in content
    assert str(policy_path) in content


def test_register_idempotent(tmp_path: Path):
    codex_home = tmp_path / "codex"
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 0.1\n", encoding="utf-8")

    first = register_codex_agent("qa-reviewer", policy_path, codex_home=codex_home)
    content_first = first.read_text(encoding="utf-8")
    second = register_codex_agent("qa-reviewer", policy_path, codex_home=codex_home)
    assert first == second
    assert second.read_text(encoding="utf-8") == content_first


def test_register_toml_injection_safety(tmp_path: Path):
    """decision policy path with special chars should not break TOML structure."""
    codex_home = tmp_path / "codex"
    tricky_path = Path('/tmp/agent with "quotes" and \\backslash/policy.yaml')

    result = register_codex_agent("safe-agent", tricky_path, codex_home=codex_home)
    assert result.exists()
    content = result.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(content)
    assert parsed["name"] == "safe-agent"


# ── keep-going start --agent / --agents CLI ─────────────────────────────────────────

def test_start_with_agent_writes_state(monkeypatch, tmp_path: Path):
    agent_home = tmp_path / "agents-home"
    agent_home.mkdir()
    _scaffold_agent(agent_home, "qa-reviewer")
    _write_template_policy(tmp_path / "artifacts")

    cfg = _config(ROOT, artifacts_dir=tmp_path / "artifacts")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(agent_home))

    state_home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()

    result = CliRunner().invoke(
        cli.main,
        [
            "start",
            "--project", str(project),
            "--agents", "qa-reviewer",
            "--state-home", str(state_home),
            "--codex-home", str(tmp_path / "codex"),
            "--agents-home", str(tmp_path / "agents-inst"),
            "--claude-home", str(tmp_path / "claude"),
            "--no-verify",
            "--register-hosts", "none",
        ],
    )
    assert result.exit_code == 0, result.output

    state_files = list(state_home.glob("*/state.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["agents"] == ["qa-reviewer"]


def test_start_with_render_mode_block(monkeypatch, tmp_path: Path):
    agent_home = tmp_path / "agents-home"
    agent_home.mkdir()
    _scaffold_agent(agent_home, "arch-keep_going")
    _write_template_policy(tmp_path / "artifacts")

    cfg = _config(ROOT, artifacts_dir=tmp_path / "artifacts")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(agent_home))

    state_home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()

    result = CliRunner().invoke(
        cli.main,
        [
            "start",
            "--project", str(project),
            "--agents", "arch-keep_going",
            "--render-mode", "block",
            "--state-home", str(state_home),
            "--codex-home", str(tmp_path / "codex"),
            "--agents-home", str(tmp_path / "agents-inst"),
            "--claude-home", str(tmp_path / "claude"),
            "--no-verify",
            "--register-hosts", "none",
        ],
    )
    assert result.exit_code == 0, result.output

    state_files = list(state_home.glob("*/state.json"))
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["render_mode"] == "block"


def test_start_without_agents_is_backward_compatible(monkeypatch, tmp_path: Path):
    _write_template_policy(tmp_path / "artifacts")

    cfg = _config(ROOT, artifacts_dir=tmp_path / "artifacts")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    state_home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()

    result = CliRunner().invoke(
        cli.main,
        [
            "start",
            "--project", str(project),
            "--state-home", str(state_home),
            "--codex-home", str(tmp_path / "codex"),
            "--agents-home", str(tmp_path / "agents-inst"),
            "--claude-home", str(tmp_path / "claude"),
            "--no-verify",
            "--register-hosts", "none",
        ],
    )
    assert result.exit_code == 0, result.output

    state_files = list(state_home.glob("*/state.json"))
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["agents"] == ["default"]


# ── npm wrapper passthrough ──────────────────────────────────────────────────

NPM_BIN = str(Path(__file__).resolve().parent.parent / "packages" / "npm" / "bin" / "keep-going.js")


def test_npm_render_mode_rejects_invalid():
    result = subprocess.run(
        [
            "node", NPM_BIN, "start",
            "--source", str(ROOT),
            "--render-mode", "invalid",
            "--project", "/tmp/x",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "render-mode must be advisory or block" in result.stderr
