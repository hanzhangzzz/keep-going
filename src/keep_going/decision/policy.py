"""Generic deterministic rules used before optional model generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    principles: tuple[str, ...]
    reply_hint: str


CHINESE_REPLY_HINTS = {
    "authorization-requires-human": "未经真人明确授权，不执行敏感操作；先说明影响范围和回滚方案。",
    "verification-before-completion": "先运行相关验证并报告命令、结果和剩余风险，再判断是否完成。",
    "low-risk-continuation": "继续。在已对齐范围内做最小必要改动，并验证最终产物。",
    "scope-boundary": "保持在明确任务边界内；相邻改进单独列出，不直接修改。",
    "verification-request": "验证最终交付物，并报告准确命令、结果和未覆盖风险。",
    "default-safe-boundary": "只做当前上下文支持的最小可逆动作；缺少事实或授权时交回真人。",
}


def match_rules(question: str, recent_context: str = "") -> list[RuleHit]:
    text = f"{question}\n{recent_context}".lower()
    hits: list[RuleHit] = []
    _append_if(
        hits,
        text,
        ("commit", "push", "delete", "production", "secret", "token", "payment", "提交", "推送", "删除", "生产", "密钥", "付款"),
        RuleHit(
            "authorization-requires-human",
            ("safety-boundary", "scope-fidelity"),
            "Do not execute the sensitive action without explicit human authorization; report impact and rollback options.",
        ),
    )
    _append_if(
        hits,
        text,
        ("completed", "fixed", "done", "完成", "修复", "做完"),
        RuleHit(
            "verification-before-completion",
            ("evidence-before-completion",),
            "Run the relevant verification and report the command, result, and remaining risk before claiming completion.",
        ),
    )
    _append_if(
        hits,
        text,
        ("continue", "proceed", "next step", "继续", "下一步", "要不要", "是否继续"),
        RuleHit(
            "low-risk-continuation",
            ("scope-fidelity", "evidence-before-completion"),
            "Continue within the agreed scope, keep the change minimal, and verify the final artifact.",
        ),
    )
    _append_if(
        hits,
        text,
        ("scope", "only change", "do not refactor", "只改", "不要重构", "不要顺手", "范围"),
        RuleHit(
            "scope-boundary",
            ("scope-fidelity",),
            "Stay inside the explicit task boundary; list adjacent improvements separately instead of changing them.",
        ),
    )
    _append_if(
        hits,
        text,
        ("verify", "test", "evidence", "verification", "验证", "测试", "证据", "验收"),
        RuleHit(
            "verification-request",
            ("evidence-before-completion",),
            "Verify the final deliverable and report the exact command, result, and uncovered risk.",
        ),
    )
    if not hits:
        hits.append(
            RuleHit(
                "default-safe-boundary",
                ("scope-fidelity", "safety-boundary"),
                "Take only the smallest reversible step supported by the available context; return control if facts or authorization are missing.",
            )
        )
    return hits


def estimate_confidence(question: str, examples: list[Any], hits: list[RuleHit]) -> float:
    if len(question.strip()) < 4:
        return 0.2
    top_score = examples[0].score if examples else 0.0
    score = 0.35 + min(top_score * 0.8, 0.3) + min(len(hits) * 0.06, 0.18)
    if any(hit.rule_id != "default-safe-boundary" for hit in hits):
        score = max(score, 0.6)
    return round(max(0.0, min(score, 0.92)), 2)


def should_escalate(question: str, confidence: float) -> bool:
    return confidence < 0.5 or _has_any(
        question.lower(),
        (
            "commit", "push", "delete", "production", "secret", "token", "payment",
            "提交", "推送", "删除", "生产", "线上", "密钥", "付款", "转账",
            "drop table", "reset --hard", "rm -rf",
        ),
    )


def draft_reply(question: str, hits: list[RuleHit], examples: list[Any], escalate: bool) -> str:
    if escalate:
        if _contains_cjk(question):
            return "这个需要真人确认。先不要执行敏感操作；请说明证据、影响范围和回滚方案。"
        return "Human confirmation is required. Do not execute the sensitive action; report evidence, impact, and rollback options."
    if _should_reuse_example(hits, examples):
        return _clip(examples[0].user_reply, 260)
    if _contains_cjk(question):
        return "；".join(_ordered_unique(CHINESE_REPLY_HINTS[hit.rule_id] for hit in hits))
    return "; ".join(_ordered_unique(hit.reply_hint for hit in hits))


def _append_if(hits: list[RuleHit], text: str, needles: tuple[str, ...], hit: RuleHit) -> None:
    if _has_any(text, needles):
        hits.append(hit)


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _should_reuse_example(hits: list[RuleHit], examples: list[Any]) -> bool:
    if not examples:
        return False
    if any(hit.rule_id != "default-safe-boundary" for hit in hits):
        return examples[0].score >= 5.0
    return examples[0].score >= 4.0


def _ordered_unique(items: Any) -> list[Any]:
    seen = set()
    output = []
    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)
    return output


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
