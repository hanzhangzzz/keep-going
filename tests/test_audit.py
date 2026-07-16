from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.audit import render_audit_markdown, run_audit
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
from keep_going.integration.package import PLUGIN_PUBLIC_FILES
from keep_going.decision.policy_runtime import compile_runtime_policy


def _decision_command() -> str:
    payload = {
        "action": "block",
        "reply": "继续跑验证。",
        "reason": "cli_model_blocked",
        "confidence": 0.9,
        "category": "verification",
        "evidence": [{"source": "test-cli", "id": "audit-smoke"}],
    }
    script = f"import json,sys; sys.stdin.read(); print({json.dumps(json.dumps(payload, ensure_ascii=False))})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


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


def _minimal_policy() -> dict[str, object]:
    return {
        "core_principles": [{"id": "x"}],
        "preferences": {},
        "redlines": [],
        "stop_decision": {
            "rules": [
                {
                    "id": "stop-no-message",
                    "category": "other",
                    "action": "allow",
                    "reason": "stop_event_without_assistant_message",
                    "confidence": 0.9,
                    "derived_from": "scope-fidelity",
                    "when": {"message_empty": True},
                },
                {
                    "id": "when-stop-hook-sees-own-feedback",
                    "category": "other",
                    "action": "allow",
                    "reason": "stop_self_feedback_allows_end",
                    "confidence": 0.95,
                    "derived_from": ["convergent-iteration", "ai-autonomy-as-north-star"],
                    "markers": ["Keep Going 已按项目级 Stop hook 代用户给出轻量决策", "请把上面内容当作用户回复继续处理"],
                },
                {
                    "id": "when-stop-hook-sees-completed-report",
                    "category": "other",
                    "action": "allow",
                    "reason": "stop_completed_report_allows_end",
                    "confidence": 0.9,
                    "derived_from": ["outcome-only-care", "convergent-iteration"],
                    "completion_markers": ["本轮完成度", "主线目标", "关键改动", "已验证", "未完成或阻塞", "下一步建议", "下一刀建议"],
                    "min_completion_markers": 3,
                    "no_pending_markers": ["没有未完成", "等待真人", "主线已完成"],
                },
                {
                    "id": "stop-risk-needs-human",
                    "category": "authorization",
                    "action": "escalate",
                    "reason": "stop_risk_needs_human",
                    "confidence": 0.92,
                    "derived_from": "outcome-only-care",
                    "principles_applied": ["outcome-only-care", "do-not-delete-unrecoverable", "do-not-leak-secrets"],
                    "heuristics_applied": ["git-write-needs-authorization"],
                    "terms": ["commit", "push", "提交", "推送", "删除", "token"],
                },
                {
                    "id": "stop-no-decision-needed",
                    "category": "other",
                    "action": "allow",
                    "reason": "stop_no_user_decision_needed",
                    "confidence": 0.8,
                    "derived_from": ["scope-fidelity", "convergent-iteration"],
                },
                {
                    "id": "stop-lightweight-decision",
                    "category": "preference",
                    "action": "block",
                    "reason": "stop_lightweight_decision",
                    "confidence": 0.8,
                    "derived_from": ["ai-autonomy-as-north-star", "outcome-only-care"],
                    "reply": "继续。按最小必要改动推进，先确保有产物级验证；不要做无关重构或未授权的 git 操作。",
                    "terms": ["要不要", "是否", "选哪个", "哪种", "验证够", "能不能提交", "commit", "push", "proceed", "?", "？"],
                    "patterns": [r"(继续|下一步).{0,12}(吗|么|\?|？)", r"(要|是否|能否|可以).{0,12}继续"],
                },
            ]
        },
    }


def _write_minimal_project(root: Path) -> Config:
    cfg = _config(root)
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    _write(
        cfg.paths.artifacts_dir / "decision-policy.template.yaml",
        yaml.safe_dump({"version": "template", "core_principles": []}),
    )
    _write(source, yaml.safe_dump(_minimal_policy(), allow_unicode=True))
    compile_runtime_policy(source)
    _write(
        cfg.paths.data_dir / "labels" / "labeled.jsonl",
        json.dumps(
            {
                "turn_id": "turn1",
                "ts": "2026-05-01T00:00:00Z",
                "project": str(root),
                "role": "user",
                "content": "继续，先验证再交付。",
                "prev_assistant": "要不要继续下一步？",
                "labels": ["execute-short", "verification-demand"],
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    for path in [
        "src/keep_going/decision/reply.py",
        "src/keep_going/decision/hook.py",
        "src/keep_going/decision/policy.py",
        "src/keep_going/eval/replay.py",
        "src/keep_going/eval/conformance.py",
        "src/keep_going/integration/install.py",
        "src/keep_going/integration/package.py",
        "src/keep_going/integration/bridge.py",
        "src/keep_going/patterns/distill.py",
        "src/keep_going/mcp_stdio.py",
    ]:
        _write(root / path, "ok")
    _write(root / ".codex/skills/keep-going/SKILL.md", "scripts/03-reply.sh scripts/04-mcp.sh keep-going bridge --input-json")
    _write(root / "plugins/keep-going/skills/keep-going/SKILL.md", "scripts/reply.sh scripts/mcp.sh scripts/bridge.sh --input-json")
    for path in [
        "scripts/03-reply.sh",
        "scripts/04-mcp.sh",
        "plugins/keep-going/scripts/reply.sh",
        "plugins/keep-going/scripts/mcp.sh",
        "plugins/keep-going/scripts/bridge.sh",
        "plugins/keep-going/hooks/keep-going-decision-hook.sh",
        "plugins/keep-going/hooks/keep-going-stop-hook.sh",
    ]:
        _write(root / path, "#!/usr/bin/env bash\n")
        (root / path).chmod(0o755)
    _write(
        root / "scripts/install-integration.sh",
        """#!/usr/bin/env bash
set -euo pipefail
codex_home=""
agents_home=""
claude_home=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex-home) codex_home="$2"; shift 2 ;;
    --agents-home) agents_home="$2"; shift 2 ;;
    --claude-home) claude_home="$2"; shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$codex_home/agents"
cp ".codex/agents/keep-going.toml" "$codex_home/agents/keep-going.toml"
mkdir -p "$codex_home/prompts"
mkdir -p "$codex_home/commands"
mkdir -p "$agents_home/plugins"
cp -R "plugins/keep-going" "$agents_home/plugins/keep-going"
printf '%s\n' "$PWD" > "$agents_home/plugins/keep-going/runtime-root"
printf '%s\n' "$PWD" > "$agents_home/plugins/keep-going/.repo-root"
mkdir -p "$claude_home/plugins/marketplaces/keep-going-local/.claude-plugin"
cp ".claude-plugin/marketplace.json" "$claude_home/plugins/marketplaces/keep-going-local/.claude-plugin/marketplace.json"
mkdir -p "$claude_home/plugins/marketplaces/keep-going-local/plugins"
cp -R "plugins/keep-going" "$claude_home/plugins/marketplaces/keep-going-local/plugins/keep-going"
printf '%s\n' "$PWD" > "$claude_home/plugins/marketplaces/keep-going-local/plugins/keep-going/runtime-root"
printf '%s\n' "$PWD" > "$claude_home/plugins/marketplaces/keep-going-local/plugins/keep-going/.repo-root"
echo "legacy Codex skill absent"
echo "codex slash commands unsupported"
echo "installed agent"
echo "installed plugin"
echo "codex marketplace registration"
echo "installed Claude plugin"
""",
    )
    (root / "scripts/install-integration.sh").chmod(0o755)
    _write(root / ".codex/agents/keep-going.toml", 'developer_instructions = "scripts/03-reply.sh scripts/04-mcp.sh"')
    _write(
        root / "plugins/keep-going/.codex-plugin/plugin.json",
        json.dumps(
            {
                "name": "keep-going",
                "skills": "./skills/",
                "commands": "./commands/",
                "hooks": "./hooks.json",
                "mcpServers": "./.mcp.json",
            }
        ),
    )
    _write(
        root / "plugins/keep-going/plugin.json",
        json.dumps(
            {
                "name": "keep-going",
                "skills": "./skills/",
                "commands": "./commands/",
                "hooks": "./hooks.json",
                "mcpServers": "./.mcp.json",
            }
        ),
    )
    _write(root / "plugins/keep-going/.claude-plugin/plugin.json", json.dumps({"name": "keep-going"}))
    _write(
        root / "plugins/keep-going/.mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "keep-going": {
                        "command": "./scripts/mcp.sh",
                        "cwd": ".",
                        "startup_timeout_sec": 60,
                    }
                }
            }
        ),
    )
    _write(
        root / "plugins/keep-going/hooks.json",
        json.dumps(
            {
                "hooks": {
                    "keep-going-decision": {
                        "command": "./hooks/keep-going-decision-hook.sh",
                        "inputSchema": {"required": ["question"]},
                    },
                    "keep-going-stop": {
                        "command": "./hooks/keep-going-stop-hook.sh",
                        "inputSchema": {"additionalProperties": True},
                    },
                },
            }
        ),
    )
    _write(
        root / "plugins/keep-going/hooks/hooks.json",
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./hooks/keep-going-stop-hook.sh"}]}]}}),
    )
    _write(root / ".agents/plugins/marketplace.json", json.dumps({"plugins": []}))
    _write(root / ".claude-plugin/marketplace.json", json.dumps({"name": "keep-going-local", "plugins": []}))
    for relative in PLUGIN_PUBLIC_FILES:
        path = root / "plugins" / "keep-going" / relative
        if not path.exists():
            _write(path, "{}" if path.suffix == ".json" else "public\n")
    return cfg


def _write_generated_eval(cfg: Config, *, score: float = 0.7, cases: int = 10) -> Path:
    path = cfg.paths.data_dir / "eval" / "eval-generated.md"
    _write(
        path,
        "\n".join(
            [
                "# Keep Going Eval",
                "",
                "- eval_scope: lightweight_decision",
                "- source_user_turns: 10",
                f"- eligible_user_turns: {cases}",
                "- holdout_ratio: 0.1",
                f"- evaluated_cases: {cases}",
                "- generated_mode: true",
                f"- avg_decision_alignment: {score:.3f}",
                "- avg_text_similarity: 0.100",
                "- avg_confidence: 0.700",
                "",
            ]
        ),
    )
    return path


def test_run_audit_reports_artifact_passes_and_policy_blockers(tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)

    report = run_audit(cfg)

    assert report["ready"] is False
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["runtime_policy"]["status"] == "PASS"
    assert "source_sha256=" in checks["runtime_policy"]["evidence"]
    assert checks["generated_eval_quality"]["status"] == "FAIL"
    assert any("generated_eval_quality" in blocker for blocker in report["blockers"])
    assert any("global-install" in blocker for blocker in report["blockers"])


def test_run_audit_fails_tampered_runtime_policy(tmp_path: Path) -> None:
    cfg = _write_minimal_project(tmp_path)
    runtime = cfg.paths.artifacts_dir / "decision-policy.runtime.yaml"
    runtime.write_text(runtime.read_text(encoding="utf-8") + "tampered: true\n", encoding="utf-8")

    report = run_audit(cfg)

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["runtime_policy"]["status"] == "FAIL"
    assert "stale or modified" in checks["runtime_policy"]["evidence"]


def test_run_audit_passes_generated_eval_quality_gate(tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    _write_generated_eval(cfg, score=0.7, cases=10)

    report = run_audit(cfg)

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["generated_eval_quality"]["status"] == "PASS"
    assert not any(blocker.startswith("generated_eval_quality:") for blocker in report["blockers"])


def test_run_audit_fails_low_generated_eval_quality(tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    _write_generated_eval(cfg, score=0.2, cases=10)

    report = run_audit(cfg)

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["generated_eval_quality"]["status"] == "FAIL"
    assert "avg_decision_alignment" in checks["generated_eval_quality"]["evidence"]


def test_run_audit_global_install_reports_missing_targets(tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)

    report = run_audit(
        cfg,
        global_install=True,
        codex_home=tmp_path / "missing-codex",
        agents_home=tmp_path / "missing-agents",
        claude_home=tmp_path / "missing-claude",
    )

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["global_installation"]["status"] == "FAIL"
    assert "agent" in checks["global_installation"]["evidence"]
    assert not any(blocker.startswith("installation:") for blocker in report["blockers"])


def test_run_audit_global_install_passes_for_installed_targets(tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    codex_home = tmp_path / "codex-home"
    agents_home = tmp_path / "agents-home"
    claude_home = tmp_path / "claude-home"
    for path in [
            codex_home / "agents" / "keep-going.toml",
            agents_home / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json",
            agents_home / "plugins" / "keep-going" / "plugin.json",
            agents_home / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json",
            agents_home / "plugins" / "keep-going" / "skills" / "keep-going" / "SKILL.md",
            agents_home / "plugins" / "keep-going" / "scripts" / "reply.sh",
            agents_home / "plugins" / "keep-going" / "scripts" / "mcp.sh",
            agents_home / "plugins" / "keep-going" / "scripts" / "bridge.sh",
            agents_home / "plugins" / "keep-going" / "hooks" / "keep-going-decision-hook.sh",
            agents_home / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh",
            agents_home / "plugins" / "keep-going" / "hooks" / "hooks.json",
            agents_home / "plugins" / "keep-going" / ".mcp.json",
            agents_home / "plugins" / "keep-going" / "runtime-root",
            agents_home / "plugins" / "keep-going" / ".repo-root",
            claude_home / "plugins" / "marketplaces" / "keep-going-local" / ".claude-plugin" / "marketplace.json",
            claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json",
            claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh",
            claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / "scripts" / "bridge.sh",
            claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / "runtime-root",
        ]:
            _write(path, "ok")
    for path in [
        agents_home / "plugins" / "keep-going" / "scripts" / "reply.sh",
        agents_home / "plugins" / "keep-going" / "scripts" / "mcp.sh",
        agents_home / "plugins" / "keep-going" / "scripts" / "bridge.sh",
        agents_home / "plugins" / "keep-going" / "hooks" / "keep-going-decision-hook.sh",
        agents_home / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh",
        claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh",
        claude_home / "plugins" / "marketplaces" / "keep-going-local" / "plugins" / "keep-going" / "scripts" / "bridge.sh",
    ]:
        path.chmod(0o755)
    (agents_home / "plugins" / "keep-going" / ".repo-root").write_text(str(tmp_path) + "\n", encoding="utf-8")
    _write(
        codex_home / "hooks.json",
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        f"KEEP_GOING_HOST=codex "
                                        f"{agents_home / 'plugins' / 'keep-going' / 'hooks' / 'keep-going-stop-hook.sh'}"
                                    ),
                                    "timeout": 360,
                                }
                            ],
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
    )

    report = run_audit(cfg, global_install=True, codex_home=codex_home, agents_home=agents_home, claude_home=claude_home)

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["global_installation"]["status"] == "PASS"
    assert not any(blocker.startswith("installation:") for blocker in report["blockers"])


def test_render_audit_markdown():
    text = render_audit_markdown(
        {"ready": False, "checks": [{"name": "x", "status": "PASS", "evidence": "ok"}], "blockers": ["b"]}
    )

    assert "Keep Going Readiness Audit" in text
    assert "| x | PASS | ok |" in text
    assert "- b" in text


def test_audit_cli_json(monkeypatch, tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(cli.main, ["audit", "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ready"] is False


def test_audit_cli_global_install_json(monkeypatch, tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(
        cli.main,
        [
            "audit",
            "--global-install",
            "--json-output",
            "--codex-home",
            str(tmp_path / "missing-codex"),
            "--agents-home",
            str(tmp_path / "missing-agents"),
            "--claude-home",
            str(tmp_path / "missing-claude"),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["global_installation"]["status"] == "FAIL"


def test_audit_cli_smoke_json(monkeypatch, tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    monkeypatch.setenv("KEEP_GOING_CLAUDE_CODE_CLI_COMMAND", _decision_command())
    monkeypatch.setenv("KEEP_GOING_CODEX_CLI_COMMAND", _decision_command())
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(cli.main, ["audit", "--smoke", "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["smoke_reply_runtime"]["status"] == "PASS"
    assert checks["smoke_hook_policy"]["status"] == "PASS"
    assert checks["smoke_mcp_runtime"]["status"] == "PASS"
    assert checks["smoke_eval_runtime"]["status"] == "PASS"
    assert checks["smoke_conformance_eval"]["status"] == "PASS"
    assert checks["smoke_distill_runtime"]["status"] == "PASS"
    assert checks["smoke_install_runtime"]["status"] == "PASS"
    assert checks["smoke_package_runtime"]["status"] == "PASS"
    assert checks["smoke_bridge_runtime"]["status"] == "PASS"


def test_run_audit_fails_when_wrapper_is_not_executable(tmp_path: Path):
    cfg = _write_minimal_project(tmp_path)
    (tmp_path / "scripts" / "03-reply.sh").chmod(0o644)

    report = run_audit(cfg)

    failed = {check["name"]: check for check in report["checks"] if check["status"] == "FAIL"}
    assert "reply_wrapper" in failed
    assert "not executable" in failed["reply_wrapper"]["evidence"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
