"""Readiness audit for Keep Going artifacts and integrations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from keep_going.config import Config
from keep_going.eval.conformance import run_conformance
from keep_going.eval.replay import run_eval
from keep_going.integration.bridge import run_self_test
from keep_going.integration.install import run_installer, verify_installation
from keep_going.integration.package import package_keep_going
from keep_going.mcp_stdio import handle_request
from keep_going.mcp_stdio import _tools
from keep_going.patterns.distill import distill_candidate
from keep_going.decision.hook import handle_hook_event
from keep_going.decision.policy_runtime import load_runtime_policy, runtime_policy_path
from keep_going.decision.reply import build_decision_reply


_GENERATED_EVAL_MIN_ALIGNMENT = 0.6
_GENERATED_EVAL_MIN_CASES = 10


@dataclass(frozen=True)
class AuditCheck:
    name: str
    status: str
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "evidence": self.evidence}


def run_audit(
    cfg: Config,
    *,
    smoke: bool = False,
    global_install: bool = False,
    codex_home: Path | None = None,
    agents_home: Path | None = None,
    claude_home: Path | None = None,
) -> dict[str, Any]:
    checks = [
        _check_policy(cfg),
        _check_runtime_policy(cfg),
        _check_labeled_data(cfg),
        _exists("distill_runtime", cfg.root / "src" / "keep_going" / "patterns" / "distill.py"),
        _exists("reply_cli", cfg.root / "src" / "keep_going" / "decision" / "reply.py"),
        _exists("hook_runtime", cfg.root / "src" / "keep_going" / "decision" / "hook.py"),
        _exists("policy_runtime", cfg.root / "src" / "keep_going" / "decision" / "policy.py"),
        _exists("eval_cli", cfg.root / "src" / "keep_going" / "eval" / "replay.py"),
        _check_generated_eval(cfg),
        _exists("conformance_eval", cfg.root / "src" / "keep_going" / "eval" / "conformance.py"),
        _exists("install_runtime", cfg.root / "src" / "keep_going" / "integration" / "install.py"),
        _exists("package_runtime", cfg.root / "src" / "keep_going" / "integration" / "package.py"),
        _exists("bridge_runtime", cfg.root / "src" / "keep_going" / "integration" / "bridge.py"),
        _exists("mcp_server", cfg.root / "src" / "keep_going" / "mcp_stdio.py"),
        _check_skill(
            "skill_artifact",
            cfg.root / ".codex" / "skills" / "keep-going" / "SKILL.md",
            required=(
                "scripts/03-reply.sh",
                "scripts/04-mcp.sh",
                "keep-going bridge",
                "scripts/onboard.sh",
                "--input-json",
            ),
        ),
        _check_agent(cfg.root / ".codex" / "agents" / "keep-going.toml"),
        _check_plugin(cfg.root / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json", name="plugin_manifest"),
        _check_plugin(cfg.root / "plugins" / "keep-going" / "plugin.json", name="plugin_root_manifest"),
        _check_claude_plugin(cfg.root / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json"),
        _check_skill(
            "plugin_skill_artifact",
            cfg.root / "plugins" / "keep-going" / "skills" / "keep-going" / "SKILL.md",
            required=(
                "scripts/reply.sh",
                "scripts/mcp.sh",
                "scripts/bridge.sh",
                "scripts/onboard.sh",
                "--input-json",
            ),
        ),
        _check_plugin_mcp(cfg.root / "plugins" / "keep-going" / ".mcp.json"),
        _check_plugin_hooks(cfg.root / "plugins" / "keep-going" / "hooks.json"),
        _check_claude_hooks(cfg.root / "plugins" / "keep-going" / "hooks" / "hooks.json"),
        _check_json("plugin_marketplace", cfg.root / ".agents" / "plugins" / "marketplace.json"),
        _check_json("claude_marketplace", cfg.root / ".claude-plugin" / "marketplace.json"),
        _check_executable("reply_wrapper", cfg.root / "scripts" / "03-reply.sh"),
        _check_executable("mcp_wrapper", cfg.root / "scripts" / "04-mcp.sh"),
        _check_executable("integration_installer", cfg.root / "scripts" / "install-integration.sh"),
        _check_executable("plugin_reply_wrapper", cfg.root / "plugins" / "keep-going" / "scripts" / "reply.sh"),
        _check_executable("plugin_mcp_wrapper", cfg.root / "plugins" / "keep-going" / "scripts" / "mcp.sh"),
        _check_executable("plugin_bridge_wrapper", cfg.root / "plugins" / "keep-going" / "scripts" / "bridge.sh"),
        _check_executable("plugin_onboard_wrapper", cfg.root / "plugins" / "keep-going" / "scripts" / "onboard.sh"),
        _check_executable("plugin_hook_wrapper", cfg.root / "plugins" / "keep-going" / "hooks" / "keep-going-decision-hook.sh"),
        _check_executable("plugin_stop_hook_wrapper", cfg.root / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh"),
        _check_mcp_tool_schema(),
    ]
    if global_install:
        checks.append(_check_global_installation(codex_home=codex_home, agents_home=agents_home, claude_home=claude_home))
    if smoke:
        checks.extend(_smoke_checks(cfg))
    blockers = _blockers(checks, global_install_checked=global_install)
    return {
        "ready": not blockers,
        "checks": [check.to_dict() for check in checks],
        "blockers": blockers,
    }


def render_audit_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Keep Going Readiness Audit",
        "",
        f"- ready: {str(report['ready']).lower()}",
        "",
        "| check | status | evidence |",
        "|---|---|---|",
    ]
    for check in report["checks"]:
        lines.append(f"| {check['name']} | {check['status']} | {_cell(check['evidence'])} |")
    if report["blockers"]:
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {blocker}" for blocker in report["blockers"])
    return "\n".join(lines) + "\n"


def _check_policy(cfg: Config) -> AuditCheck:
    path = cfg.paths.artifacts_dir / "decision-policy.yaml"
    if not path.exists():
        return AuditCheck("decision_policy", "FAIL", f"missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    principles = data.get("core_principles") if isinstance(data, dict) else None
    if not isinstance(principles, list) or not principles:
        return AuditCheck("decision_policy", "FAIL", f"invalid or empty core_principles: {path}")
    stop_section = (data.get("stop_decision") or data.get("stop_hook_decision")) if isinstance(data, dict) else None
    stop_rules = stop_section.get("rules") if isinstance(stop_section, dict) else None
    if not isinstance(stop_rules, list) or not stop_rules:
        return AuditCheck("decision_policy", "FAIL", f"missing stop_decision.rules: {path}")
    rules = [rule for rule in stop_rules if isinstance(rule, dict)]
    actions = {str(rule.get("action") or "").strip().lower() for rule in rules}
    human_only_escalate = any(
        str(rule.get("category") or "").strip().lower() in {"authorization", "information"}
        and str(rule.get("action") or "").strip().lower() == "escalate"
        for rule in rules
    )
    missing_capabilities = []
    if "allow" not in actions:
        missing_capabilities.append("allow")
    if "block" not in actions:
        missing_capabilities.append("block")
    if not human_only_escalate:
        missing_capabilities.append("human-only escalation")
    if missing_capabilities:
        return AuditCheck("decision_policy", "FAIL", f"missing stop policy capabilities: {', '.join(missing_capabilities)}")
    return AuditCheck("decision_policy", "PASS", f"{path} core_principles={len(principles)} stop_hook_rules={len(stop_rules)}")


def _check_runtime_policy(cfg: Config) -> AuditCheck:
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    runtime = runtime_policy_path(source)
    try:
        load_runtime_policy(source)
    except (FileNotFoundError, ValueError) as exc:
        return AuditCheck("runtime_policy", "FAIL", str(exc))
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    return AuditCheck(
        "runtime_policy",
        "PASS",
        f"{runtime} source_sha256={source_sha} runtime_sha256={runtime_sha}",
    )


def _check_labeled_data(cfg: Config) -> AuditCheck:
    path = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    if not path.exists():
        return AuditCheck("labeled_turns", "FAIL", f"missing: {path}")
    count = 0
    with path.open(encoding="utf-8") as f:
        for count, _ in enumerate(f, 1):
            pass
    if count == 0:
        return AuditCheck("labeled_turns", "FAIL", f"empty: {path}")
    return AuditCheck("labeled_turns", "PASS", f"{path} rows={count}")


def _check_generated_eval(cfg: Config) -> AuditCheck:
    eval_dir = cfg.paths.data_dir / "eval"
    policy_path = runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml")
    if not eval_dir.exists():
        return AuditCheck(
            "generated_eval_quality",
            "FAIL",
            f"missing generated eval report; run `uv run keep-going eval --generate --holdout-ratio 0.1 --limit {_GENERATED_EVAL_MIN_CASES}`",
        )
    policy_mtime = policy_path.stat().st_mtime if policy_path.exists() else 0.0
    reports = []
    for path in sorted(eval_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if _parse_eval_bool(text, "generated_mode") is not True:
            continue
        reports.append((path, _parse_eval_number(text, "avg_decision_alignment"), _parse_eval_int(text, "evaluated_cases")))
    if not reports:
        return AuditCheck(
            "generated_eval_quality",
            "FAIL",
            f"no generated eval report in {eval_dir}; run `uv run keep-going eval --generate --holdout-ratio 0.1 --limit {_GENERATED_EVAL_MIN_CASES}`",
        )
    fresh_reports = [(path, score, cases) for path, score, cases in reports if path.stat().st_mtime >= policy_mtime]
    if not fresh_reports:
        latest = max(path for path, _, _ in reports)
        return AuditCheck("generated_eval_quality", "FAIL", f"generated eval is older than decision policy: latest={latest.name}")
    best_path, best_score, best_cases = max(fresh_reports, key=lambda item: (item[1], item[2], item[0].stat().st_mtime))
    if best_cases < _GENERATED_EVAL_MIN_CASES:
        return AuditCheck(
            "generated_eval_quality",
            "FAIL",
            f"{best_path.name} evaluated_cases={best_cases} < {_GENERATED_EVAL_MIN_CASES}",
        )
    if best_score < _GENERATED_EVAL_MIN_ALIGNMENT:
        return AuditCheck(
            "generated_eval_quality",
            "FAIL",
            f"{best_path.name} avg_decision_alignment={best_score:.3f} < {_GENERATED_EVAL_MIN_ALIGNMENT:.3f}",
        )
    return AuditCheck(
        "generated_eval_quality",
        "PASS",
        f"{best_path} generated=true cases={best_cases} avg_decision_alignment={best_score:.3f}",
    )


def _parse_eval_bool(text: str, field: str) -> bool | None:
    match = re.search(rf"^- {re.escape(field)}:\s*(true|false)\s*$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1) == "true"


def _parse_eval_number(text: str, field: str) -> float:
    match = re.search(rf"^- {re.escape(field)}:\s*([0-9]+(?:\.[0-9]+)?)\s*$", text, re.MULTILINE)
    return float(match.group(1)) if match else 0.0


def _parse_eval_int(text: str, field: str) -> int:
    match = re.search(rf"^- {re.escape(field)}:\s*([0-9]+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else 0


def _check_global_installation(*, codex_home: Path | None, agents_home: Path | None, claude_home: Path | None) -> AuditCheck:
    report = verify_installation(codex_home=codex_home, agents_home=agents_home, claude_home=claude_home)
    if report["ok"]:
        return AuditCheck(
            "global_installation",
            "PASS",
            f"codex_home={report['codex_home']} agents_home={report['agents_home']} claude_home={report['claude_home']}",
        )
    missing = ",".join(check["name"] for check in report["checks"] if check["status"] != "PASS")
    return AuditCheck(
        "global_installation",
        "FAIL",
        f"missing={missing} codex_home={report['codex_home']} agents_home={report['agents_home']} claude_home={report['claude_home']}",
    )


def _exists(name: str, path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck(name, "FAIL", f"missing: {path}")
    return AuditCheck(name, "PASS", str(path))


def _check_executable(name: str, path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck(name, "FAIL", f"missing: {path}")
    if not path.is_file():
        return AuditCheck(name, "FAIL", f"not a file: {path}")
    if not os.access(path, os.X_OK):
        return AuditCheck(name, "FAIL", f"not executable: {path}")
    return AuditCheck(name, "PASS", f"{path} executable")


def _check_skill(name: str, path: Path, *, required: tuple[str, ...]) -> AuditCheck:
    if not path.exists():
        return AuditCheck(name, "FAIL", f"missing: {path}")
    text = path.read_text(encoding="utf-8")
    missing = [item for item in required if item not in text]
    if missing:
        return AuditCheck(name, "FAIL", f"missing references {missing}: {path}")
    return AuditCheck(name, "PASS", str(path))


def _check_agent(path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck("agent_artifact", "FAIL", f"missing: {path}")
    text = path.read_text(encoding="utf-8")
    if "scripts/03-reply.sh" not in text or "scripts/04-mcp.sh" not in text:
        return AuditCheck("agent_artifact", "FAIL", f"missing wrapper references: {path}")
    return AuditCheck("agent_artifact", "PASS", str(path))


def _check_plugin(path: Path, *, name: str = "plugin_manifest") -> AuditCheck:
    if not path.exists():
        return AuditCheck(name, "FAIL", f"missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["name", "skills", "hooks", "mcpServers"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        return AuditCheck(name, "FAIL", f"missing keys {missing}: {path}")
    if "[TODO:" in json.dumps(data, ensure_ascii=False):
        return AuditCheck(name, "FAIL", f"contains TODO placeholders: {path}")
    return AuditCheck(name, "PASS", str(path))


def _check_claude_plugin(path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck("claude_plugin_manifest", "FAIL", f"missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("name") != "keep-going":
        return AuditCheck("claude_plugin_manifest", "FAIL", f"unexpected name: {path}")
    if "[TODO:" in json.dumps(data, ensure_ascii=False):
        return AuditCheck("claude_plugin_manifest", "FAIL", f"contains TODO placeholders: {path}")
    return AuditCheck("claude_plugin_manifest", "PASS", str(path))


def _check_plugin_mcp(path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck("plugin_mcp", "FAIL", f"missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    server = (data.get("mcpServers") or {}).get("keep-going") if isinstance(data, dict) else None
    command = server.get("command") if isinstance(server, dict) else None
    if not isinstance(command, str) or not command:
        return AuditCheck("plugin_mcp", "FAIL", f"missing keep-going command: {path}")
    if server.get("cwd") != ".":
        return AuditCheck("plugin_mcp", "FAIL", f"keep-going cwd must be plugin root '.': {path}")
    timeout = server.get("startup_timeout_sec")
    if not isinstance(timeout, int) or timeout < 60:
        return AuditCheck("plugin_mcp", "FAIL", f"startup_timeout_sec must be at least 60: {path}")
    command_path = (path.parent / command).resolve()
    if not command_path.exists():
        return AuditCheck("plugin_mcp", "FAIL", f"command target missing: {command_path}")
    return AuditCheck("plugin_mcp", "PASS", f"{path} command={command} cwd=. startup_timeout_sec={timeout}")


def _check_plugin_hooks(path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck("plugin_hooks", "FAIL", f"missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    hook = (data.get("hooks") or {}).get("keep-going-decision") if isinstance(data, dict) else None
    command = hook.get("command") if isinstance(hook, dict) else None
    if not isinstance(command, str) or not command:
        return AuditCheck("plugin_hooks", "FAIL", f"missing keep-going-decision command: {path}")
    command_path = (path.parent / command).resolve()
    if not command_path.exists():
        return AuditCheck("plugin_hooks", "FAIL", f"command target missing: {command_path}")
    required = ((hook.get("inputSchema") or {}).get("required") or []) if isinstance(hook, dict) else []
    if "question" not in required:
        return AuditCheck("plugin_hooks", "FAIL", f"hook inputSchema must require question: {path}")
    stop_hook = (data.get("hooks") or {}).get("keep-going-stop") if isinstance(data, dict) else None
    stop_command = stop_hook.get("command") if isinstance(stop_hook, dict) else None
    if not isinstance(stop_command, str) or not stop_command:
        return AuditCheck("plugin_hooks", "FAIL", f"missing keep-going-stop command: {path}")
    stop_command_path = (path.parent / stop_command).resolve()
    if not stop_command_path.exists():
        return AuditCheck("plugin_hooks", "FAIL", f"stop command target missing: {stop_command_path}")
    return AuditCheck("plugin_hooks", "PASS", f"{path} command={command} stop_command={stop_command}")


def _check_claude_hooks(path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck("claude_stop_hooks", "FAIL", f"missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    stop_hooks = ((data.get("hooks") or {}).get("Stop") or []) if isinstance(data, dict) else []
    commands = []
    for group in stop_hooks:
        for hook in group.get("hooks") or []:
            command = hook.get("command")
            if isinstance(command, str):
                commands.append(command)
    if not any("keep-going-stop-hook.sh" in command for command in commands):
        return AuditCheck("claude_stop_hooks", "FAIL", f"Stop hook missing keep-going-stop-hook.sh: {path}")
    return AuditCheck("claude_stop_hooks", "PASS", str(path))


def _check_mcp_tool_schema() -> AuditCheck:
    tools = {str(tool.get("name")): tool for tool in _tools()}
    missing = sorted({"keep_going_reply", "keep_going_eval"} - tools.keys())
    if missing:
        return AuditCheck("mcp_tool_schema", "FAIL", f"missing tools: {missing}")
    reply_props = ((tools["keep_going_reply"].get("inputSchema") or {}).get("properties") or {})
    eval_props = ((tools["keep_going_eval"].get("inputSchema") or {}).get("properties") or {})
    if "question" not in reply_props:
        return AuditCheck("mcp_tool_schema", "FAIL", "keep_going_reply missing question input")
    if "generate" not in reply_props or "generate" not in eval_props:
        return AuditCheck("mcp_tool_schema", "FAIL", "generate flag must exist on keep_going_reply and keep_going_eval")
    return AuditCheck("mcp_tool_schema", "PASS", "tools=keep_going_reply,keep_going_eval generate=true supported")


def _check_json(name: str, path: Path) -> AuditCheck:
    if not path.exists():
        return AuditCheck(name, "FAIL", f"missing: {path}")
    json.loads(path.read_text(encoding="utf-8"))
    return AuditCheck(name, "PASS", str(path))


def _smoke_checks(cfg: Config) -> list[AuditCheck]:
    return [
        _smoke("smoke_reply_runtime", lambda: _smoke_reply(cfg)),
        _smoke("smoke_hook_policy", lambda: _smoke_hook_policy(cfg)),
        _smoke("smoke_mcp_runtime", lambda: _smoke_mcp(cfg)),
        _smoke("smoke_eval_runtime", lambda: _smoke_eval(cfg)),
        _smoke("smoke_conformance_eval", lambda: _smoke_conformance(cfg)),
        _smoke("smoke_distill_runtime", lambda: _smoke_distill(cfg)),
        _smoke("smoke_install_runtime", lambda: _smoke_install(cfg)),
        _smoke("smoke_package_runtime", lambda: _smoke_package(cfg)),
        _smoke("smoke_bridge_runtime", lambda: _smoke_bridge(cfg)),
        _smoke("smoke_agent_registry", lambda: _smoke_agent_registry(cfg)),
    ]


def _smoke(name: str, fn: Any) -> AuditCheck:
    try:
        evidence = str(fn())
    except Exception as exc:  # noqa: BLE001
        return AuditCheck(name, "FAIL", str(exc))
    return AuditCheck(name, "PASS", evidence)


def _smoke_reply(cfg: Config) -> str:
    result = build_decision_reply(
        question="要不要继续下一步？",
        project=str(cfg.root),
        policy_path=runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml"),
        examples_path=_examples_path(cfg),
        top_k=3,
        model=cfg.models.decision,
    )
    if not result.get("reply"):
        raise ValueError("empty reply")
    if "用户决策 decision policy" not in str(result.get("prompt") or ""):
        raise ValueError("prompt missing decision policy sections")
    return f"reply={_clip(str(result['reply']), 80)}"


def _smoke_hook_policy(cfg: Config) -> str:
    explicit = handle_hook_event(cfg, {"question": "要不要继续下一步？", "project": str(cfg.root)})
    noop = handle_hook_event(cfg, {"prompt": "帮我写一个普通函数", "project": str(cfg.root)})
    danger = handle_hook_event(
        cfg,
        {"hook_event_name": "PreToolUse", "tool_name": "bash", "tool_input": {"command": "git push origin main"}},
    )
    if not explicit.get("continuation_injected"):
        raise ValueError("explicit question did not ask Keep Going")
    if noop.get("continuation_injected"):
        raise ValueError("non-decision event should no-op")
    if not danger.get("escalate"):
        raise ValueError("dangerous tool event did not escalate")
    return "explicit=ask noop=noop dangerous=escalate"


def _smoke_mcp(cfg: Config) -> str:
    tools = handle_request(cfg, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in tools["result"]["tools"]}
    if names != {"keep_going_reply", "keep_going_eval"}:
        raise ValueError(f"unexpected tools: {sorted(names)}")
    reply = handle_request(
        cfg,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "keep_going_reply", "arguments": {"question": "要不要继续下一步？", "project": str(cfg.root)}},
        },
    )
    payload = json.loads(reply["result"]["content"][0]["text"])
    if not payload.get("reply"):
        raise ValueError("empty MCP keep_going_reply payload")
    return "tools=keep_going_reply,keep_going_eval call=keep_going_reply"


def _smoke_eval(cfg: Config) -> str:
    with tempfile.TemporaryDirectory(prefix="keep-going-audit-") as tmp:
        out = Path(tmp) / "eval.md"
        path = run_eval(cfg, holdout_ratio=0.01, limit=1, top_k=3, out_path=out)
        text = path.read_text(encoding="utf-8")
        if "generated_mode: false" not in text:
            raise ValueError("local eval report missing generated_mode=false")
        return f"report={path.name}"


def _smoke_conformance(cfg: Config) -> str:
    with tempfile.TemporaryDirectory(prefix="keep-going-audit-") as tmp:
        out = Path(tmp) / "conformance.md"
        report = run_conformance(cfg, out_path=out, top_k=3)
        if not report["passed"]:
            raise ValueError(f"{report['passed_cases']} / {report['total_cases']} cases passed")
        return f"cases={report['passed_cases']}/{report['total_cases']}"


def _smoke_distill(cfg: Config) -> str:
    with tempfile.TemporaryDirectory(prefix="keep-going-audit-") as tmp:
        out = Path(tmp) / "candidate.yaml"
        path = distill_candidate(cfg, out_path=out, limit_per_signal=1)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data.get("status") != "candidate":
            raise ValueError("candidate decision policy status missing")
        return f"candidate={path.name}"


def _smoke_install(cfg: Config) -> str:
    with tempfile.TemporaryDirectory(prefix="keep-going-install-") as tmp:
        target = Path(tmp) / "codex-home"
        agents_home = Path(tmp) / "agents-home"
        claude_home = Path(tmp) / "claude-home"
        output = run_installer(cfg, codex_home=target, agents_home=agents_home, claude_home=claude_home, execute=True)
        report = verify_installation(codex_home=target, agents_home=agents_home, claude_home=claude_home)
        if not report["ok"]:
            missing = ",".join(check["name"] for check in report["checks"] if check["status"] != "PASS")
            raise ValueError(f"install verification failed: {missing}")
        required_markers = (
            "legacy Codex skill",
            "codex slash commands unsupported",
            "installed agent",
            "installed plugin",
        )
        if not all(marker in output for marker in required_markers):
            raise ValueError("installer output missing success markers")
        return "temp_codex_home=installed temp_agents_home=installed temp_claude_home=installed"


def _smoke_package(cfg: Config) -> str:
    with tempfile.TemporaryDirectory(prefix="keep-going-package-") as tmp:
        out = Path(tmp) / "keep-going-package"
        path = package_keep_going(cfg, out_dir=out)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        required = [
            path / "artifacts" / "decision-policy.template.yaml",
            path / "skills" / "keep-going" / "SKILL.md",
            path / "agents" / "keep-going.toml",
            path / "plugins" / "keep-going" / ".codex-plugin" / "plugin.json",
            path / "plugins" / "keep-going" / "plugin.json",
            path / "plugins" / "keep-going" / ".claude-plugin" / "plugin.json",
            path / "plugins" / "keep-going" / "hooks" / "keep-going-stop-hook.sh",
        ]
        missing = [str(item) for item in required if not item.exists()]
        if missing:
            raise ValueError(f"package files missing: {missing}")
        if manifest.get("privacy", {}).get("includes_raw_logs") is not False:
            raise ValueError("package manifest must declare raw logs are excluded")
        if manifest.get("privacy", {}).get("includes_private_policy") is not False:
            raise ValueError("package manifest must declare private decision policy is excluded")
        forbidden = [
            path / "decision-policy.yaml",
            path / "decision-policy.runtime.yaml",
            path / "plugins" / "keep-going" / "runtime-root",
            path / "plugins" / "keep-going" / ".repo-root",
        ]
        leaked = [str(item) for item in forbidden if item.exists()]
        if leaked:
            raise ValueError(f"package contains private or host-bound files: {leaked}")
        return "package=keep-going-package"


def _smoke_bridge(cfg: Config) -> str:
    report = run_self_test(cfg)
    if not report["passed"]:
        raise ValueError(json.dumps(report, ensure_ascii=False))
    return f"disabled={report['disabled_action']} enabled={report['enabled_action']}"


def _smoke_agent_registry(cfg: Config) -> str:
    from keep_going.agents.registry import validate_agent_name, resolve_agent, list_agents

    for name in ("default", "my-test-agent", "qa-reviewer"):
        result = validate_agent_name(name)
        if name == "default" and result["ok"]:
            raise ValueError("default should be rejected as reserved")
        if name != "default" and not result["ok"]:
            raise ValueError(f"{name} should be valid: {result['reason']}")

    canonical = cfg.paths.artifacts_dir / "decision-policy.yaml"
    resolved = resolve_agent("default", project=str(cfg.root), canonical_policy=canonical)
    if not resolved["valid"]:
        raise ValueError(f"default agent resolution failed: {resolved['reason']}")

    agents = list_agents(scope="global")
    return f"validate=ok resolve_default={resolved['scope']} global_agents={len(agents)}"


def _examples_path(cfg: Config) -> Path:
    labels = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    turns = cfg.paths.data_dir / "turns" / "turns.jsonl"
    return labels if labels.exists() else turns


def _blockers(checks: list[AuditCheck], *, global_install_checked: bool) -> list[str]:
    blockers = [f"{check.name}: {check.evidence}" for check in checks if check.status == "FAIL"]
    if not global_install_checked:
        blockers.append("installation: run `uv run keep-going audit --global-install` or `uv run keep-going install --execute` after approving global config writes")
    return blockers


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"
