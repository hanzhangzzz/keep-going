"""Hook event policy for deciding when to ask the Keep Going."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from keep_going.config import Config
from keep_going.decision.policy_runtime import runtime_policy_path
from keep_going.decision.reply import build_decision_reply


DECISION_TERMS = (
    "要不要",
    "是否",
    "继续",
    "下一步",
    "选哪个",
    "哪种",
    "方案",
    "验证够",
    "能不能提交",
    "commit",
    "push",
    "proceed",
)

DANGEROUS_TERMS = (
    "commit",
    "push",
    "提交",
    "推送",
    "建分支",
    "rm -rf",
    "reset --hard",
    "删除",
    "生产",
    "线上",
    "密钥",
    "token",
    "付款",
    "转账",
)


def handle_hook_event(
    cfg: Config,
    event: dict[str, Any],
    *,
    top_k: int = 5,
    generate: bool = False,
    policy_path: str | Path | None = None,
) -> dict[str, Any]:
    """Decide whether to ask the Keep Going for this hook event.

    ``policy_path`` (U2): when ``None`` (default), falls back to the
    canonical decision policy at ``cfg.paths.artifacts_dir / "decision-policy.yaml"``.
    When given, the alternative decision policy is loaded for the lookup. Lets the
    bridge's per-project agent selection (U5) override the canonical keep_going.
    """
    question = _extract_question(event)
    project = _extract_project(event)
    recent_context = _extract_recent_context(event)
    reason = "explicit_question" if _explicit_question(event) else "decision_signal"

    if not question:
        return _no_op("no decision signal")

    labels = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    turns = cfg.paths.data_dir / "turns" / "turns.jsonl"
    if policy_path is None:
        policy_path = runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml")
    policy_path = Path(policy_path).expanduser()
    result = build_decision_reply(
        question=question,
        project=project,
        policy_path=policy_path,
        examples_path=labels if labels.exists() else turns,
        recent_context=recent_context,
        top_k=top_k,
        model=cfg.models.decision,
        generate=generate,
    )
    result.update(
        {
            "continuation_injected": True,
            "hook_reason": reason,
            "hook_event": str(event.get("hook_event_name") or event.get("event_name") or event.get("event") or ""),
        }
    )
    return result


def parse_hook_event(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid hook input JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("hook input JSON must be an object")
    return payload


def _extract_question(event: dict[str, Any]) -> str:
    for key in ("question", "message"):
        value = str(event.get(key) or "").strip()
        if value:
            return value

    for key in ("last_assistant_message", "assistant_message", "assistant_response", "rawOutput"):
        value = str(event.get(key) or "").strip()
        if value and _has_decision_signal(value):
            return value

    prompt = str(event.get("prompt") or event.get("user_prompt") or "").strip()
    if _has_decision_signal(prompt):
        return prompt

    tool_text = _tool_text(event)
    if _has_danger_signal(tool_text):
        return f"是否允许执行高风险工具操作：{tool_text}"
    if _has_decision_signal(tool_text):
        return tool_text
    return ""


def _extract_project(event: dict[str, Any]) -> str:
    for key in ("project", "cwd", "workspace", "repo"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return ""


def _extract_recent_context(event: dict[str, Any]) -> str:
    direct = str(event.get("recent_context") or event.get("context") or "").strip()
    if direct:
        return direct
    tool_text = _tool_text(event)
    if tool_text:
        return f"hook_tool: {tool_text}"
    return ""


def _tool_text(event: dict[str, Any]) -> str:
    tool_name = str(event.get("tool_name") or event.get("tool") or "")
    tool_input = event.get("tool_input") or event.get("input") or {}
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or tool_input.get("cmd") or tool_input.get("path") or "")
        return " ".join(part for part in (tool_name, command) if part).strip()
    return " ".join(part for part in (tool_name, str(tool_input or "")) if part).strip()


def _explicit_question(event: dict[str, Any]) -> bool:
    return any(str(event.get(key) or "").strip() for key in ("question", "message"))


def _has_decision_signal(text: str) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in DECISION_TERMS)


def _has_danger_signal(text: str) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in DANGEROUS_TERMS)


def _no_op(reason: str) -> dict[str, Any]:
    return {
        "continuation_injected": False,
        "hook_reason": reason,
        "reply": "",
        "confidence": 0.0,
        "escalate": False,
        "principles_applied": [],
        "heuristics_applied": [],
        "few_shots": [],
        "generated": False,
    }
