"""Deterministic safety boundary for Stop-hook continuation decisions."""

from __future__ import annotations

import math
from typing import Any, Mapping


HUMAN_ONLY_CATEGORIES = {"authorization", "information"}
VALID_ACTIONS = {"allow", "block", "escalate"}
MIN_BLOCK_CONFIDENCE = 0.6
RISK_MARKERS = (
    "commit", "push", "force push", "push --force", "reset --hard", "git reset",
    "rm -rf", "delete", "删除", "提交", "推送", "生产", "production",
    "deploy", "部署", "permission", "权限", "chmod", "chown", "secret",
    "密钥", "api key", "credential", "token", "payment", "付款",
)
KEEP_GOING_INJECTION_MARKERS = (
    "Keep Going 已按项目级 Stop hook 代用户给出轻量决策",
    "请把上面内容当作用户回复继续处理",
)
EVENT_MESSAGE_KEYS = (
    "question",
    "message",
    "last_assistant_message",
    "assistant_message",
    "assistant_response",
    "rawOutput",
)
KEEP_GOING_FIXED_BOILERPLATE = (
    "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：",
    "请把上面内容当作用户回复继续处理；",
    "如果后续触及提交、推送、删除、生产或密钥等高风险动作，仍需真人确认。",
    "如果后续触及提交、推送等高风险动作，仍需真人确认。",
)


def detect_deterministic_risks(text: Any) -> list[str]:
    """Detect sensitive actions without relying on the model's category."""
    lowered = str(text or "").lower()
    return [f"mentions:{marker}" for marker in RISK_MARKERS if marker in lowered]


def event_message_texts(event: Mapping[str, Any]) -> list[str]:
    """Return every host-supported current-message value in precedence order."""
    return [str(event[key]).strip() for key in EVENT_MESSAGE_KEYS if str(event.get(key) or "").strip()]


def enforce_stop_safety_policy(
    model_decision: Mapping[str, Any],
    decision_context: Mapping[str, Any] | None,
    *,
    max_chain_depth: int | None = None,
    current_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a decision that cannot inject an unsafe continuation."""
    decision = dict(model_decision)
    context = dict(decision_context or {})
    reason = _escalation_reason(
        decision,
        context,
        max_chain_depth=max_chain_depth,
        current_event=current_event,
    )
    if reason is None:
        return decision
    return {
        **decision,
        "action": "escalate",
        "reply": "",
        "reason": reason,
        "evidence": [
            *_as_evidence(decision.get("evidence")),
            {"source": "stop_safety", "id": reason, "kind": "deterministic_gate"},
        ],
    }


def _escalation_reason(
    decision: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    max_chain_depth: int | None,
    current_event: Mapping[str, Any] | None,
) -> str | None:
    action = str(decision.get("action") or "").strip().lower()
    category = str(decision.get("category") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return "safety_gate_invalid_action"
    if category in HUMAN_ONLY_CATEGORIES:
        return f"safety_gate_human_only_category:{category}"
    risk_flags = [*_context_risk_flags(context), *_event_risk_flags(current_event or {})]
    risk_flags = list(dict.fromkeys(risk_flags))
    if risk_flags:
        return "safety_gate_risk_flags:" + ",".join(risk_flags)
    chain_depth = _non_negative_int(context.get("continuation_chain_depth"))
    if max_chain_depth is not None and chain_depth >= max_chain_depth:
        return f"safety_gate_chain_depth:{chain_depth}/{max_chain_depth}"
    if action != "block":
        return None
    if not category:
        return "safety_gate_missing_category"
    if _context_unavailable(context):
        return "safety_gate_context_unavailable"
    if _confidence(decision.get("confidence")) < MIN_BLOCK_CONFIDENCE:
        return "safety_gate_low_confidence"
    if not str(decision.get("reply") or "").strip():
        return "safety_gate_empty_block_reply"
    return None


def _context_unavailable(context: Mapping[str, Any]) -> bool:
    confidence = str(context.get("context_confidence") or "").strip().lower()
    status = str(context.get("context_status") or "").strip().lower()
    return confidence == "low" or status in {"missing_transcript", "read_error"}


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    parsed = float(value)
    return parsed if math.isfinite(parsed) else 0.0


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_evidence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _context_risk_flags(context: Mapping[str, Any]) -> list[str]:
    flags = [str(flag).strip() for flag in context.get("risk_flags", []) if str(flag).strip()]
    for key in ("latest_user_goal", "current_progress", "pending_question"):
        flags.extend(detect_deterministic_risks(context.get(key)))
    for item in context.get("recent_commands", []):
        text = item.get("text") if isinstance(item, Mapping) else item
        flags.extend(detect_deterministic_risks(text))
    return list(dict.fromkeys(flags))


def _event_risk_flags(event: Mapping[str, Any]) -> list[str]:
    flags: list[str] = []
    for text in event_message_texts(event):
        flags.extend(detect_deterministic_risks(_strip_keep_going_boilerplate(text)))
    for key in (
        "command",
        "tool_input",
        "tool_name",
        "tool",
        "args",
    ):
        value = event.get(key)
        if isinstance(value, Mapping):
            for nested in value.values():
                flags.extend(detect_deterministic_risks(nested))
        elif isinstance(value, (list, tuple)):
            for item in value:
                flags.extend(detect_deterministic_risks(item))
        else:
            flags.extend(detect_deterministic_risks(value))
    return flags


def _strip_keep_going_boilerplate(text: str) -> str:
    cleaned = text
    for fragment in KEEP_GOING_FIXED_BOILERPLATE:
        cleaned = cleaned.replace(fragment, "")
    return cleaned
