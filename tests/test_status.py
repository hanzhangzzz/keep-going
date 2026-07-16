from __future__ import annotations

import json
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


def _write_healthy_policy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 0.4,
                "core_principles": [
                    {"id": "evidence-first", "statement": "结论要带证据。"},
                    {"id": "scope-fidelity", "statement": "只做当前要求的事。"},
                    {"id": "ai-autonomy", "statement": "接住轻量决策权。"},
                ],
                "preferences": {},
                "redlines": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def _write_stub_policy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"version": 0.4, "core_principles": [{"id": "p1", "statement": "test"}]}),
        encoding="utf-8",
    )


def _make_global_agent(agents_home: Path, name: str) -> Path:
    agent_dir = agents_home / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    policy = agent_dir / "policy-20260101T000000Z.yaml"
    _write_healthy_policy(policy)
    (agent_dir / "meta.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": f"agent {name}",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "current_policy": str(policy),
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    return policy


def test_status_json_healthy_canonical(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_healthy_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    compile_runtime_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "agents"))
    monkeypatch.setenv("KEEP_GOING_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(cli.main, ["status", "--project", str(tmp_path), "--json-output"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["canonical_policy"]["exists"] is True
    assert payload["canonical_policy"]["principles"] == 3
    assert payload["canonical_policy"]["looks_stub"] is False
    assert payload["runtime_policy"]["valid"] is True
    assert payload["runtime_policy"]["runtime_schema_version"] == 1
    assert len(payload["runtime_policy"]["source_sha256"]) == 64
    assert len(payload["runtime_policy"]["runtime_sha256"]) == 64
    assert payload["project"]["bound_agents"] == ["default"]


def test_status_flags_stub_canonical(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_stub_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "agents"))
    monkeypatch.setenv("KEEP_GOING_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(cli.main, ["status", "--project", str(tmp_path), "--json-output"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["canonical_policy"]["looks_stub"] is True


def test_status_lists_named_agents(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_healthy_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    agents_home = tmp_path / "agents"
    _make_global_agent(agents_home, "qa-reviewer")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(agents_home))
    monkeypatch.setenv("KEEP_GOING_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(cli.main, ["status", "--project", str(tmp_path), "--json-output"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = [a["name"] for a in payload["named_agents"]]
    assert "qa-reviewer" in names


def test_status_human_view_flags_stub(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_stub_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "agents"))
    monkeypatch.setenv("KEEP_GOING_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(cli.main, ["status", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "疑似占位" in result.output
    assert "myself" in result.output


def test_status_human_view_shows_runtime_binding_and_hashes(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    _write_healthy_policy(source)
    compile_runtime_policy(source)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "agents"))
    monkeypatch.setenv("KEEP_GOING_STATE_HOME", str(tmp_path / "state"))

    result = CliRunner().invoke(cli.main, ["status", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "decision-policy.runtime.yaml (compiled from canonical)" in result.output
    assert "source_sha256:" in result.output
    assert "runtime_sha256:" in result.output


def test_reply_agent_routes_to_named_policy(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_healthy_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    agents_home = tmp_path / "agents"
    _make_global_agent(agents_home, "qa-reviewer")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(agents_home))

    captured: dict[str, object] = {}

    def _fake_answer(**kwargs):
        captured.update(kwargs)
        return {"reply": "继续。", "confidence": 0.8, "model": "keep-going-model"}

    monkeypatch.setattr(cli, "build_decision_reply", _fake_answer)

    result = CliRunner().invoke(
        cli.main,
        ["reply", "-q", "继续吗", "--agent", "qa-reviewer", "--project", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "agents" in str(captured["policy_path"])
    assert "qa-reviewer" in str(captured["policy_path"])


def test_reply_no_agent_uses_persisted_runtime(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_healthy_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    captured: dict[str, object] = {}

    def _fake_answer(**kwargs):
        captured.update(kwargs)
        return {"reply": "继续。", "confidence": 0.8, "model": "keep-going-model"}

    monkeypatch.setattr(cli, "build_decision_reply", _fake_answer)

    result = CliRunner().invoke(cli.main, ["reply", "-q", "继续吗", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert str(captured["policy_path"]).endswith("decision-policy.runtime.yaml")


def test_reply_unknown_agent_errors(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_healthy_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "agents"))

    result = CliRunner().invoke(
        cli.main,
        ["reply", "-q", "继续吗", "--agent", "no-such-agent", "--project", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "not found" in result.output
