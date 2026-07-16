"""Prompt formatting helpers for decision policy."""

from __future__ import annotations

from typing import Any


def format_policy_prompt_sections(policy: dict[str, Any]) -> str:
    sections = [
        ("核心 decision policy", _format_principles(policy.get("core_principles", []))),
        ("战略框架", _format_strategic_frame(policy.get("strategic_frame", {}))),
        ("当前门控", _format_gates(policy.get("current_state_gates", []))),
        ("偏好", _format_preferences(policy.get("preferences", {}))),
        ("情境启发", _format_heuristics(policy.get("heuristics", []))),
        ("协作模式", _format_modes(policy.get("ai_collaboration_modes", []))),
        ("红线", _format_redlines(policy.get("redlines", []))),
        ("语气词汇", _format_vocabulary(policy.get("vocabulary", {}))),
    ]
    return "\n\n".join(f"{title}：\n{body}" for title, body in sections if body and body != "- (missing)")


def _format_principles(rows: Any) -> str:
    if not isinstance(rows, list):
        return "- (missing)"
    lines = []
    for row in rows[:8]:
        if isinstance(row, dict):
            lines.append(f"- {row.get('id')}: {_clip(str(row.get('statement', '')), 220)}")
    return "\n".join(lines) or "- (missing)"


def _format_gates(rows: Any) -> str:
    if not isinstance(rows, list):
        return "- (missing)"
    lines = []
    for row in rows[:5]:
        if isinstance(row, dict):
            lines.append(f"- {row.get('id')}: {_clip(str(row.get('gate', '')), 180)}")
    return "\n".join(lines) or "- (missing)"


def _format_preferences(groups: Any) -> str:
    if not isinstance(groups, dict):
        return "- (missing)"
    lines = []
    for group, rows in groups.items():
        if not isinstance(rows, list):
            continue
        for row in rows[:3]:
            if isinstance(row, dict):
                lines.append(f"- {group}.{row.get('id')}: {_clip(str(row.get('pref', '')), 160)}")
    return "\n".join(lines[:12]) or "- (missing)"


def _format_heuristics(rows: Any) -> str:
    if not isinstance(rows, list):
        return "- (missing)"
    lines = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        response = row.get("typical_response") or row.get("typical_choice") or row.get("reply_hint") or ""
        lines.append(f"- {row.get('id')}: 触发={_clip(str(row.get('trigger', '')), 80)}；应对={_clip(str(response), 160)}")
    return "\n".join(lines) or "- (missing)"


def _format_modes(rows: Any) -> str:
    if not isinstance(rows, list):
        return "- (missing)"
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        should = row.get("ai_should") or []
        if isinstance(should, list):
            should_text = " / ".join(str(item) for item in should[:3])
        else:
            should_text = str(should)
        lines.append(f"- {row.get('id')}: when={_clip(str(row.get('when', '')), 80)}；do={_clip(should_text, 140)}")
    return "\n".join(lines) or "- (missing)"


def _format_redlines(rows: Any) -> str:
    if not isinstance(rows, list):
        return "- (missing)"
    lines = []
    for row in rows[:6]:
        if isinstance(row, dict):
            lines.append(f"- {row.get('id')}: {_clip(str(row.get('rule', '')), 160)}")
    return "\n".join(lines) or "- (missing)"


def _format_vocabulary(vocab: Any) -> str:
    if not isinstance(vocab, dict):
        return "- (missing)"
    parts = []
    for key in ("approve_short", "reject_short", "probe", "technical_idioms", "tone"):
        values = vocab.get(key)
        if isinstance(values, list):
            parts.append(f"- {key}: {' / '.join(str(value) for value in values[:8])}")
    return "\n".join(parts) or "- (missing)"


def _format_strategic_frame(frame: Any) -> str:
    if not isinstance(frame, dict):
        return "- (missing)"
    lines = []
    for key in ("who_i_am", "what_i_value", "keep_going_mission", "long_term_north_star"):
        value = frame.get(key)
        if isinstance(value, dict):
            value = "; ".join(f"{k}={v}" for k, v in value.items())
        if value:
            lines.append(f"- {key}: {_clip(str(value), 220)}")
    return "\n".join(lines) or "- (missing)"


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"
