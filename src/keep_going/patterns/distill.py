"""Deterministic decision policy candidate distillation from labeled turns."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from keep_going.config import Config


CORE_PRINCIPLES: tuple[dict[str, Any], ...] = (
    {
        "id": "scope-fidelity",
        "statement": "只做当前要求的事，不顺手扩展、不顺手重构。",
        "signals": ("scope-correction", "rejection"),
    },
    {
        "id": "evidence-first",
        "statement": "结论要带证据；未验证不能声称完成。",
        "signals": ("verification-demand", "evidence-probe", "failure-mode"),
    },
    {
        "id": "occams-razor",
        "statement": "如非必要勿增实体；优先复用现有机制和最小可验证改动。",
        "signals": ("tool-evaluation", "choice-among-options", "scope-correction"),
    },
    {
        "id": "outcome-only-care",
        "statement": "最终交付质量优先于过程好看；验证面向真实产物。",
        "signals": ("delivery-finalize", "verification-demand"),
    },
    {
        "id": "signal-density",
        "statement": "输出先给结论，信息密度高，避免铺垫和套话。",
        "signals": ("writing-style",),
    },
    {
        "id": "ai-autonomy-as-north-star",
        "statement": "目标是让 AI 独立闭环更多执行期判断，只把真正高风险事项升级给真人。",
        "signals": ("ai-collab-meta", "meta-self-reflection", "task-kickoff"),
    },
)

CURRENT_STATE_GATES: tuple[dict[str, Any], ...] = (
    {
        "id": "git-write-needs-authorization",
        "gate": "commit / push / 建分支需要用户明确授权；已授权也只提交本次相关文件。",
        "signals": ("delivery-finalize",),
    },
    {
        "id": "dangerous-operation-needs-human",
        "gate": "删除、生产 API、密钥、付款、系统配置等高风险操作必须升级真人确认。",
        "signals": ("failure-mode", "verification-demand"),
    },
    {
        "id": "verify-before-completion",
        "gate": "交付前必须给产物级验证命令和结果；不能验证就标未验证。",
        "signals": ("verification-demand", "evidence-probe", "delivery-finalize"),
    },
)

HEURISTICS: tuple[dict[str, Any], ...] = (
    {
        "id": "when-ai-claims-fix-without-evidence",
        "trigger": "AI 声称完成、修好了、应该可以",
        "reply_hint": "先给证据链和最终产物验证；未验证不要说完成。",
        "signals": ("verification-demand", "evidence-probe", "failure-mode"),
    },
    {
        "id": "when-multiple-options",
        "trigger": "AI 给多个方案或问选哪个",
        "reply_hint": "选最小可验证、复用现有机制、能解释根因的一项。",
        "signals": ("choice-among-options", "tool-evaluation"),
    },
    {
        "id": "when-scope-drifts",
        "trigger": "AI 扩展范围、顺手改无关内容",
        "reply_hint": "收回到当前目标；无关优化列 follow-up，不直接动手。",
        "signals": ("scope-correction", "rejection"),
    },
    {
        "id": "when-user-says-continue",
        "trigger": "用户短确认继续或开始",
        "reply_hint": "继续执行到闭环，保留验证证据，不重复询问。",
        "signals": ("execute-short", "delivery-finalize"),
    },
)

PREFERENCES: dict[str, tuple[dict[str, Any], ...]] = {
    "coding": (
        {
            "id": "minimal-diff",
            "pref": "最小必要改动，避免无需求支撑的抽象和兼容分支。",
            "signals": ("scope-correction", "rejection"),
        },
        {
            "id": "product-level-verification",
            "pref": "测试 / lint / smoke 要覆盖最终交付物，而不是只做自我 review。",
            "signals": ("verification-demand", "delivery-finalize"),
        },
    ),
    "workflow": (
        {
            "id": "agent-team-loop",
            "pref": "长任务优先拆成角色清晰的 agent / skill / hook / verifier 调用面。",
            "signals": ("task-kickoff", "ai-collab-meta"),
        },
    ),
    "writing": (
        {
            "id": "concise-first",
            "pref": "文案先给主结论和价值，不要冗长铺垫。",
            "signals": ("writing-style",),
        },
    ),
}

REDLINES: tuple[dict[str, Any], ...] = (
    {
        "id": "do-not-fake-completion",
        "rule": "不能把未验证、部分完成、不可复现的状态说成已完成。",
        "signals": ("verification-demand", "evidence-probe"),
    },
    {
        "id": "do-not-break-environment",
        "rule": "不能为完成局部任务破坏用户环境、全局配置或生产数据。",
        "signals": ("failure-mode", "scope-correction"),
    },
)

VOCABULARY = ["闭环", "收口", "证据链", "最小必要改动", "不要顺手", "产物级验证", "先读后写"]


def distill_candidate(cfg: Config, *, out_path: Path | None = None, limit_per_signal: int = 5) -> Path:
    if limit_per_signal < 1:
        raise ValueError("limit_per_signal must be >= 1")
    labeled_path = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    if not labeled_path.exists():
        raise FileNotFoundError(f"labeled turns not found: {labeled_path}; run `keep-going classify` first")
    rows = _load_rows(labeled_path)
    if not rows:
        raise ValueError(f"no user rows found: {labeled_path}")

    label_counts = Counter(label for row in rows for label in row.get("labels", []))
    document = {
        "version": 0.5,
        "status": "candidate",
        "distill_mode": "deterministic-label-baseline",
        "generated_at": datetime.now().date().isoformat(),
        "source": _relative(labeled_path, cfg.root),
        "window": {"days": cfg.window.days},
        "coverage": {
            "total_user_turns": len(rows),
            "labeled_user_turns": sum(1 for row in rows if row.get("labels")),
            "label_counts": dict(label_counts.most_common()),
        },
        "core_principles": [_with_evidence(item, rows, limit_per_signal) for item in CORE_PRINCIPLES],
        "current_state_gates": [_with_evidence(item, rows, limit_per_signal) for item in CURRENT_STATE_GATES],
        "preferences": {
            group: [_with_evidence(item, rows, limit_per_signal) for item in items]
            for group, items in PREFERENCES.items()
        },
        "heuristics": [_with_evidence(item, rows, limit_per_signal) for item in HEURISTICS],
        "redlines": [_with_evidence(item, rows, limit_per_signal) for item in REDLINES],
        "vocabulary": VOCABULARY,
        "gaps": _gaps(rows, label_counts),
        "changelog": [
            {
                "version": 0.5,
                "change": "由 labeled.jsonl 生成 deterministic candidate；用于人审或 LLM 二次蒸馏，不自动覆盖 canonical decision policy。",
            }
        ],
    }
    out = out_path or cfg.paths.artifacts_dir / "decision-policy.candidate.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("role") == "user":
                rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("ts", "")))


def _with_evidence(item: dict[str, Any], rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    signals = tuple(item.get("signals", ()))
    out = {key: value for key, value in item.items() if key != "signals"}
    out["derived_from_labels"] = list(signals)
    out["evidence_turn_ids"] = _evidence(rows, signals, limit)
    return out


def _evidence(rows: list[dict[str, Any]], signals: tuple[str, ...], limit: int) -> list[str]:
    if not signals:
        return []
    signal_set = set(signals)
    found = []
    for row in reversed(rows):
        if signal_set.isdisjoint(set(row.get("labels") or [])):
            continue
        turn_id = str(row.get("turn_id") or "")
        if turn_id:
            found.append(turn_id)
        if len(found) >= limit:
            break
    return list(reversed(found))


def _gaps(rows: list[dict[str, Any]], label_counts: Counter[str]) -> list[str]:
    gaps = []
    unlabeled = len([row for row in rows if not row.get("labels")])
    if rows and unlabeled / len(rows) > 0.4:
        gaps.append("仍有大量 user turns 未被规则标签覆盖，自动蒸馏需要继续提升 label coverage。")
    if label_counts.get("choice-among-options", 0) < 10:
        gaps.append("多方案选择样本偏少，Keep Going 在 option selection 上需要更多人工审阅样本。")
    if label_counts.get("execute-short", 0) < 20:
        gaps.append("短确认 / 放权推进样本偏少，继续/暂停边界需通过真实调用面校准。")
    return gaps


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
