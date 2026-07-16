"""U4 tests: CLI agent subcommand group (new / list / show / delete / edit)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.agents.registry import load_meta, save_meta


# ── helpers ──────────────────────────────────────────────────────────────────

def _agent_home(tmp_path: Path) -> Path:
    home = tmp_path / "agents-home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "my-project"
    proj.mkdir()
    return proj


def _config(tmp_path: Path):
    from keep_going.config import (
        Config, FiltersCfg, ModelsCfg, PathsCfg, ReasoningCfg,
        ScrubCfg, SourcesCfg, WindowCfg,
    )
    return Config(
        window=WindowCfg(days=90),
        sources=SourcesCfg(
            claude_code_dir=tmp_path / "claude",
            codex_archived_dir=tmp_path / "codex",
            codex_history=tmp_path / "history.jsonl",
        ),
        paths=PathsCfg(data_dir=tmp_path / "data", artifacts_dir=tmp_path / "artifacts"),
        scrub=ScrubCfg(enabled=True, user_replacement="USER"),
        models=ModelsCfg(reasoning="r", eval="e", decision="t"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=tmp_path,
    )


def _write_template(root: Path) -> Path:
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    template = artifacts / "decision-policy.template.yaml"
    template.write_text(
        yaml.safe_dump(
            {
                "version": 0.1,
                "core_principles": [{"id": "p1", "statement": "test principle"}],
                "preferences": [],
                "redlines": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return template


# ── agent new ────────────────────────────────────────────────────────────────

def test_agent_new_creates_dir_and_meta(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    result = CliRunner().invoke(cli.main, ["agent", "new", "qa-reviewer", "--description", "QA policy reviewer"])

    assert result.exit_code == 0, result.output
    agent_dir = _agent_home(tmp_path) / "qa-reviewer"
    assert agent_dir.is_dir()
    meta = load_meta(agent_dir)
    assert meta["name"] == "qa-reviewer"
    assert meta["description"] == "QA policy reviewer"
    assert meta["current_policy"].endswith(".yaml")
    assert Path(meta["current_policy"]).is_file()


def test_agent_new_rejects_duplicate_without_force(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "qa-reviewer"])
    result = CliRunner().invoke(cli.main, ["agent", "new", "qa-reviewer"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_agent_new_force_backs_up_and_rebuilds(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "qa-reviewer"])
    result = CliRunner().invoke(cli.main, ["agent", "new", "qa-reviewer", "--force"])
    assert result.exit_code == 0, result.output
    assert "backed up" in result.output
    agent_dir = _agent_home(tmp_path) / "qa-reviewer"
    assert agent_dir.is_dir()


def test_agent_new_rejects_reserved_name(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))

    result = CliRunner().invoke(cli.main, ["agent", "new", "default"])
    assert result.exit_code != 0
    assert "reserved" in result.output


def test_agent_new_rejects_empty_name(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))

    result = CliRunner().invoke(cli.main, ["agent", "new", ""])
    assert result.exit_code != 0


def test_agent_new_scope_project(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    project = _project(tmp_path)
    _write_template(tmp_path)

    result = CliRunner().invoke(
        cli.main,
        ["agent", "new", "proj-agent", "--scope", "project", "--project", str(project)],
    )
    assert result.exit_code == 0, result.output
    agent_dir = project / ".keep-going" / "agents" / "proj-agent"
    assert agent_dir.is_dir()
    meta = load_meta(agent_dir)
    assert meta["scope"] == "project"


# ── agent list ───────────────────────────────────────────────────────────────

def test_agent_list_shows_agents(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "alpha"])
    CliRunner().invoke(cli.main, ["agent", "new", "beta"])

    result = CliRunner().invoke(cli.main, ["agent", "list"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "beta" in result.output


def test_agent_list_empty(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))

    result = CliRunner().invoke(cli.main, ["agent", "list"])
    assert result.exit_code == 0
    assert "no agents found" in result.output


# ── agent show ───────────────────────────────────────────────────────────────

def test_agent_show_prints_meta_and_policy(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "viewer"])
    result = CliRunner().invoke(cli.main, ["agent", "show", "viewer"])
    assert result.exit_code == 0, result.output
    assert "viewer" in result.output
    assert "meta.json" in result.output


def test_agent_show_rejects_unknown(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))

    result = CliRunner().invoke(cli.main, ["agent", "show", "nonexistent"])
    assert result.exit_code != 0
    assert "not found" in result.output


# ── agent delete ─────────────────────────────────────────────────────────────

def test_agent_delete_moves_to_trash(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "to-delete"])
    agent_dir = _agent_home(tmp_path) / "to-delete"
    assert agent_dir.is_dir()

    result = CliRunner().invoke(cli.main, ["agent", "delete", "to-delete"])
    assert result.exit_code == 0, result.output
    assert not agent_dir.exists()
    assert "trash" in result.output
    trash_dir = _agent_home(tmp_path) / ".trash"
    assert trash_dir.is_dir()
    assert any("to-delete" in d.name for d in trash_dir.iterdir())


def test_agent_delete_purge(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "to-purge"])
    agent_dir = _agent_home(tmp_path) / "to-purge"

    result = CliRunner().invoke(cli.main, ["agent", "delete", "to-purge", "--purge"])
    assert result.exit_code == 0, result.output
    assert not agent_dir.exists()
    assert "permanently deleted" in result.output


def test_agent_delete_refuses_default(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))

    result = CliRunner().invoke(cli.main, ["agent", "delete", "default"])
    assert result.exit_code != 0
    assert "cannot delete the default" in result.output


# ── agent edit ───────────────────────────────────────────────────────────────

def test_agent_edit_valid_policy_succeeds(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "editable"])
    agent_dir = _agent_home(tmp_path) / "editable"
    meta = load_meta(agent_dir)
    policy_path = Path(meta["current_policy"])

    def fake_editor(cmd_args):
        policy_path.write_text(
            yaml.safe_dump(
                {
                    "core_principles": [{"id": "edited", "statement": "edited principle"}],
                    "preferences": [{"id": "p1", "pref": "test"}],
                    "redlines": [{"id": "r1", "rule": "test"}],
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        return 0

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.side_effect = lambda args, **kwargs: type("R", (), {"returncode": 0})()
        result = CliRunner().invoke(cli.main, ["agent", "edit", "editable"])

    assert result.exit_code == 0, result.output


def test_agent_edit_invalid_policy_restores_backup(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(_agent_home(tmp_path)))
    _write_template(tmp_path)

    CliRunner().invoke(cli.main, ["agent", "new", "bad-edit"])
    agent_dir = _agent_home(tmp_path) / "bad-edit"
    meta = load_meta(agent_dir)
    policy_path = Path(meta["current_policy"])
    original_content = policy_path.read_text(encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 0})()

        def corrupt_policy(*args, **kwargs):
            policy_path.write_text("not: valid\nmissing_sections: true\n", encoding="utf-8")
            return type("R", (), {"returncode": 0})()

        mock_run.side_effect = corrupt_policy
        result = CliRunner().invoke(cli.main, ["agent", "edit", "bad-edit"])

    assert result.exit_code != 0
    assert "validation failed" in result.output
    assert policy_path.read_text(encoding="utf-8") == original_content
