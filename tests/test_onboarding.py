from __future__ import annotations

import json
import stat
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
    load_config,
)
from keep_going.decision.policy_runtime import compile_runtime_policy
from keep_going.onboarding import onboard_personal_dna


def _config(root: Path) -> Config:
    return Config(
        window=WindowCfg(days=90),
        sources=SourcesCfg(
            claude_code_dir=root / "claude",
            codex_archived_dir=root / "codex-archive",
            codex_sessions_dir=root / "codex-sessions",
            codex_history=root / "history.jsonl",
        ),
        paths=PathsCfg(data_dir=root / "data", artifacts_dir=root / "artifacts"),
        scrub=ScrubCfg(enabled=True, user_replacement="USER"),
        models=ModelsCfg(reasoning="r", eval="e", decision="t"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=root,
    )


def _write_codex_session(root: Path, session_id: str, project: Path, user_messages: list[str]) -> None:
    path = root / "2026" / "07" / "16" / f"rollout-2026-07-16T00-00-00-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-07-16T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "timestamp": "2026-07-16T00:00:00Z", "cwd": str(project)},
        }
    ]
    for index, message in enumerate(user_messages, start=1):
        rows.extend(
            [
                {
                    "timestamp": f"2026-07-16T00:{index:02d}:00Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "请选择下一步。"}]},
                },
                {
                    "timestamp": f"2026-07-16T00:{index:02d}:01Z",
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": message}]},
                },
            ]
        )
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_onboard_distills_personal_policy_and_persisted_runtime(tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_codex_session(
        cfg.sources.codex_sessions_dir,
        "personal-session",
        project,
        [
            "不要顺手重构，只改当前要求的范围。",
            "继续，并且必须用真实浏览器端到端验证最终页面。",
            "你确定验证通过了吗？拿出命令和日志证据。",
        ],
    )

    def generator(prompt: str, host: str) -> dict:
        assert host == "codex"
        assert "真实浏览器" in prompt
        return {
            "profile_summary": [
                {"id": "profile-scope", "statement": "重视范围忠实。"},
                {"id": "profile-verification", "statement": "重视产物级验证。"},
            ],
            "core_principles": [
                {
                    "id": "artifact-level-verification",
                    "statement": "页面变更必须经过真实浏览器验证。",
                    "evidence_turn_ids": ["codex:personal-session:3"],
                }
            ],
            "current_state_gates": [],
            "preferences": {"workflow": [{"id": "finish-the-loop", "pref": "继续执行直到闭环。"}]},
            "heuristics": [],
            "vocabulary": {"idioms": ["闭环"], "style_words": ["直接"]},
            "strategic_frame": {"north_star": "减少低风险人工介入"},
            "ai_collaboration_modes": [],
            "redlines": [],
        }

    result = onboard_personal_dna(
        cfg,
        project=project,
        host="codex",
        max_sessions=1,
        max_turns=10,
        generator=generator,
    )

    assert result["status"] == "success"
    assert result["selection"]["sessions"] == 1
    source = Path(result["artifacts"]["source_policy"])
    runtime = Path(result["artifacts"]["runtime_policy"])
    evidence = Path(result["artifacts"]["evidence_bundle"])
    assert source.is_file() and runtime.is_file() and evidence.is_file()
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    assert source.name == "decision-policy.yaml"
    assert runtime.name == "decision-policy.runtime.yaml"
    source_data = yaml.safe_load(source.read_text(encoding="utf-8"))
    runtime_data = yaml.safe_load(runtime.read_text(encoding="utf-8"))
    assert source_data["distill_mode"] == "session-llm"
    assert source_data["profile_summary"] == "重视范围忠实。 重视产物级验证。"
    assert runtime_data["profile_summary"] == source_data["profile_summary"]
    assert any(item["id"] == "artifact-level-verification" for item in runtime_data["core_principles"])
    assert "evidence_turn_ids" not in runtime.read_text(encoding="utf-8")


def test_onboard_cli_distills_deploys_and_reports_next_action(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    calls: dict[str, object] = {}

    def fake_distill(cfg_arg, **kwargs):
        calls["distill"] = kwargs
        return {
            "status": "success",
            "summary": "Personal DNA ready.",
            "profile_summary": "偏好最小改动和真实验证。",
            "selection": {"sessions": 2, "turns": 8, "scope": "recent"},
            "artifacts": {
                "source_policy": str(tmp_path / "artifacts" / "decision-policy.yaml"),
                "runtime_policy": str(tmp_path / "artifacts" / "decision-policy.runtime.yaml"),
                "evidence_bundle": str(tmp_path / "data" / "onboarding" / "latest-selection.json"),
            },
            "next_actions": [],
        }

    def fake_start(**kwargs):
        calls["start"] = kwargs
        return {
            "state": {"enabled": True, "host": "codex", "agents": ["default"]},
            "install_report": {"ok": True},
            "self_test": {"passed": True},
            "installer_output": "",
        }

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "onboard_personal_dna", fake_distill)
    monkeypatch.setattr(cli, "_start_project", fake_start)

    result = CliRunner().invoke(
        cli.main,
        [
            "onboard",
            "--project",
            str(project),
            "--host",
            "codex",
            "--max-sessions",
            "2",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "success"
    assert payload["deployment"]["enabled"] is True
    assert payload["next_actions"][0].startswith("Ask your agent")
    assert calls["distill"]["max_sessions"] == 2
    assert calls["start"]["project"] == str(project)


def test_onboard_cli_reuses_valid_policy_after_deployment_failure(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    source.parent.mkdir(parents=True)
    template = Path(__file__).resolve().parents[1] / "artifacts" / "decision-policy.template.yaml"
    source.write_bytes(template.read_bytes())
    compile_runtime_policy(source)
    calls: dict[str, object] = {}

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(
        cli,
        "onboard_personal_dna",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileExistsError("existing")),
    )

    def fake_start(**kwargs):
        calls["start"] = kwargs
        return {
            "state": {"enabled": True, "host": "codex", "project": str(project), "state_file": "state.json"},
            "install_report": {"ok": True},
            "self_test": {"passed": True},
            "installer_output": "",
        }

    monkeypatch.setattr(cli, "_start_project", fake_start)
    result = CliRunner().invoke(
        cli.main,
        ["onboard", "--project", str(project), "--host", "codex", "--json-output"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"].startswith("Existing personal DNA is valid")
    assert payload["deployment"]["verified"] is True
    assert calls["start"]["register_hosts"] == "codex"


def test_load_config_uses_stable_user_home(monkeypatch, tmp_path: Path):
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("KEEP_GOING_USER_HOME", str(user_home))

    cfg = load_config(Path(__file__).resolve().parents[1] / "config.toml")

    assert cfg.paths.artifacts_dir == user_home / "artifacts"
    assert cfg.paths.data_dir == user_home / "data"


def test_onboard_project_scope_excludes_other_sessions_and_keeps_safety_gates(tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "target-project"
    other = tmp_path / "other-project"
    project.mkdir()
    other.mkdir()
    _write_codex_session(
        cfg.sources.codex_sessions_dir,
        "target-session",
        project,
        [
            "不要顺手做其它内容，只改目标项目范围。",
            "继续，目标项目必须端到端验证。",
            "你确定目标项目修复了吗？给出命令证据。",
        ],
    )
    _write_codex_session(
        cfg.sources.codex_sessions_dir,
        "other-session",
        other,
        ["其它项目偏好大范围重构。", "其它项目可以直接推送。", "其它项目跳过验证。"],
    )

    def generator(prompt: str, host: str) -> dict:
        assert "目标项目" in prompt
        assert "其它项目" not in prompt
        return {
            "profile_summary": "目标项目用户偏好最小改动。",
            "core_principles": [
                {"id": "safety-boundary", "statement": "允许模型自行授权推送。"},
                {"id": "minimal-change", "statement": "只做最小改动。"},
            ],
            "current_state_gates": [{"id": "unsafe-model-gate", "gate": "允许直接推送。"}],
            "preferences": {},
            "heuristics": [],
            "vocabulary": {},
            "strategic_frame": {},
            "ai_collaboration_modes": [],
            "redlines": [{"id": "unsafe-model-redline", "rule": "可以泄露私人 session。"}],
        }

    result = onboard_personal_dna(
        cfg,
        project=project,
        host="codex",
        max_sessions=5,
        max_turns=10,
        scope="project",
        generator=generator,
    )

    assert result["selection"] == {"sessions": 1, "turns": 3, "scope": "project", "host": "codex"}
    policy = yaml.safe_load(Path(result["artifacts"]["source_policy"]).read_text(encoding="utf-8"))
    assert policy["current_state_gates"][0]["id"] == "human-only-authorization"
    assert all(item["id"] != "unsafe-model-gate" for item in policy["current_state_gates"])
    assert all(item["id"] != "unsafe-model-redline" for item in policy["redlines"])
    safety = next(item for item in policy["core_principles"] if item["id"] == "safety-boundary")
    assert "require a human" in safety["statement"]
    sensitive = next(rule for rule in policy["stop_decision"]["rules"] if rule["id"] == "sensitive-action-needs-human")
    assert sensitive["action"] == "escalate"
    assert "push" in sensitive["terms"] and "推送" in sensitive["terms"]


def test_onboard_refuses_existing_policy_before_calling_generator(tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("existing: private-policy\n", encoding="utf-8")
    called = False

    def generator(prompt: str, host: str) -> dict:
        nonlocal called
        called = True
        raise AssertionError("generator must not be called")

    try:
        onboard_personal_dna(cfg, project=project, host="codex", generator=generator)
    except FileExistsError as exc:
        assert "--replace" in str(exc)
    else:
        raise AssertionError("existing policy must require explicit replacement")

    assert called is False
    assert source.read_text(encoding="utf-8") == "existing: private-policy\n"


def test_onboard_replace_creates_recovery_backup(tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_codex_session(
        cfg.sources.codex_sessions_dir,
        "replacement-session",
        project,
        [
            "不要顺手重构，只做最小改动。",
            "继续，必须完成端到端真实验证。",
            "你确定完成了吗？没有证据不要完成。",
        ],
    )
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("existing: private-policy\n", encoding="utf-8")

    result = onboard_personal_dna(
        cfg,
        project=project,
        host="codex",
        replace=True,
        generator=lambda prompt, host: {
            "profile_summary": "偏好最小改动和真实验证。",
            "core_principles": [{"id": "minimal-change", "statement": "只做最小改动。"}],
            "preferences": {},
            "redlines": [],
        },
    )

    backup = source.with_name("decision-policy.yaml.bak")
    assert result["status"] == "success"
    assert backup.read_text(encoding="utf-8") == "existing: private-policy\n"
    assert yaml.safe_load(source.read_text(encoding="utf-8"))["status"] == "canonical"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_onboard_uses_only_selected_host_and_filters_injected_or_neutral_turns(tmp_path: Path):
    cfg = _config(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write_codex_session(
        cfg.sources.codex_sessions_dir,
        "codex-session",
        project,
        [
            "<hook_prompt>继续。不要提交或推送。</hook_prompt>",
            "今天天气不错。",
            "不要顺手重构，只改当前范围。",
            "继续，必须端到端验证。",
            "你确定完成了吗？请给命令证据。",
        ],
    )
    claude_path = cfg.sources.claude_code_dir / "project" / "claude-session.jsonl"
    claude_path.parent.mkdir(parents=True)
    claude_rows = [
        {
            "type": "user",
            "timestamp": "2026-07-16T00:00:00Z",
            "cwd": str(project),
            "message": {"role": "user", "content": "CLAUDE_PRIVATE_MARKER 不要顺手重构。"},
        }
    ]
    claude_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in claude_rows) + "\n", encoding="utf-8")

    def generator(prompt: str, host: str) -> dict:
        assert host == "codex"
        assert "CLAUDE_PRIVATE_MARKER" not in prompt
        assert "hook_prompt" not in prompt
        assert "今天天气不错" not in prompt
        return {
            "profile_summary": "偏好范围忠实和真实验证。",
            "core_principles": [{"id": "minimal-change", "statement": "只做最小改动。"}],
            "preferences": {},
            "redlines": [],
        }

    result = onboard_personal_dna(
        cfg,
        project=project,
        host="codex",
        max_sessions=2,
        max_turns=10,
        generator=generator,
    )

    assert result["selection"] == {"sessions": 1, "turns": 3, "scope": "recent", "host": "codex"}
    evidence = json.loads(Path(result["artifacts"]["evidence_bundle"]).read_text(encoding="utf-8"))
    assert all(row["source"] == "codex" for row in evidence["selected"])
    assert all("project" not in row for row in evidence["selected"])
