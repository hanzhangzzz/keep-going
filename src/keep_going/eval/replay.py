"""Local replay evaluation for the Keep Going reply runtime."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from keep_going.config import Config
from keep_going.decision.policy_runtime import runtime_policy_path
from keep_going.decision.reply import _clip, _tokens, build_decision_reply


_DECISION_LABELS = {
    "choice-among-options",
    "execute-short",
    "verification-demand",
    "evidence-probe",
    "scope-correction",
    "interrupt-rollback",
    "rejection",
}
_DECISION_PROMPT_NEEDLES = (
    "要不要",
    "是否",
    "继续",
    "下一步",
    "选",
    "方案",
    "哪种",
    "确认",
    "可以吗",
    "怎么做",
    "要我",
    "是否要",
    "should",
    "proceed",
    "option",
    "plan",
)
_MAX_LIGHTWEIGHT_DECISION_CHARS = 120


@dataclass(frozen=True)
class EvalCase:
    turn_id: str
    project: str
    actual: str
    predicted: str
    confidence: float
    escalate: bool
    similarity: float
    decision_alignment: float


def run_eval(
    cfg: Config,
    *,
    holdout_ratio: float = 0.1,
    limit: int | None = 30,
    top_k: int = 5,
    out_path: Path | None = None,
    generate: bool = False,
    generator: Callable[[str, str], str] | None = None,
) -> Path:
    if not 0 < holdout_ratio <= 1:
        raise ValueError("holdout_ratio must be in (0, 1]")
    labeled_path = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    if not labeled_path.exists():
        raise FileNotFoundError(f"labeled turns not found: {labeled_path}; run `keep-going classify` first")
    source_rows = _load_source_rows(labeled_path)
    rows = [row for row in source_rows if _is_decision_eval_row(row)]
    if not rows:
        raise ValueError("no eligible lightweight decision turns with prev_assistant found")

    holdout = _select_holdout(rows, holdout_ratio, limit)
    excluded = {str(row.get("turn_id", "")) for row in holdout}
    cases = [_evaluate_row(cfg, labeled_path, row, excluded, top_k, generate, generator) for row in holdout]
    out = out_path or cfg.paths.data_dir / "eval" / f"eval-{datetime.now().date().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        _render_report(
            cases,
            source_count=len(source_rows),
            eligible_count=len(rows),
            holdout_ratio=holdout_ratio,
            generate=generate,
        ),
        encoding="utf-8",
    )
    return out


def _load_source_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("role") == "user" and row.get("prev_assistant") and row.get("content"):
                rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("ts", "")))


def _is_decision_eval_row(row: dict[str, Any]) -> bool:
    content = str(row.get("content") or "")
    if _is_noise_response(content):
        return False
    if len(content) > _MAX_LIGHTWEIGHT_DECISION_CHARS:
        return False
    labels = set(row.get("labels") or [])
    if labels & _DECISION_LABELS:
        return True
    prompt = str(row.get("prev_assistant") or "").lower()
    return _decision_kind(content) != "unknown" and any(needle.lower() in prompt for needle in _DECISION_PROMPT_NEEDLES)


def _is_noise_response(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in {"undefined", "null", "none"}:
        return True
    if lowered.startswith("stop hook feedback:"):
        return True
    if stripped.startswith("[") and (" ssl_error " in lowered or " http=" in lowered):
        return True
    return False


def _select_holdout(rows: list[dict[str, Any]], holdout_ratio: float, limit: int | None) -> list[dict[str, Any]]:
    count = max(1, int(len(rows) * holdout_ratio))
    selected = _dedupe_by_content(rows[-count:])
    if limit is not None:
        selected = selected[:limit]
    return selected


def _dedupe_by_content(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scheduled prompts (cron re-fires) repeat the same user content dozens of
    times with a slightly different assistant context each round; evaluating one
    decision N times measures generation variance, not decision policy quality. Keep the
    first occurrence of each distinct content."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = " ".join(str(row.get("content") or "").split())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _evaluate_row(
    cfg: Config,
    labeled_path: Path,
    row: dict[str, Any],
    excluded: set[str],
    top_k: int,
    generate: bool,
    generator: Callable[[str, str], str] | None,
) -> EvalCase:
    result = build_decision_reply(
        question=str(row.get("prev_assistant") or ""),
        project=str(row.get("project") or ""),
        policy_path=runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml"),
        examples_path=labeled_path,
        top_k=top_k,
        model=cfg.models.decision,
        exclude_turn_ids=excluded,
        generate=generate,
        generator=generator,
    )
    actual = str(row.get("content") or "")
    predicted = str(result["reply"])
    similarity = _similarity(actual, predicted)
    alignment = _decision_alignment(
        actual,
        predicted,
        bool(result["escalate"]),
        similarity,
        actual_kind=_row_decision_kind(row),
    )
    return EvalCase(
        turn_id=str(row.get("turn_id", "")),
        project=str(row.get("project", "")),
        actual=actual,
        predicted=predicted,
        confidence=float(result["confidence"]),
        escalate=bool(result["escalate"]),
        similarity=similarity,
        decision_alignment=alignment,
    )


def _similarity(actual: str, predicted: str) -> float:
    actual_tokens = _tokens(actual)
    predicted_tokens = _tokens(predicted)
    if not actual_tokens or not predicted_tokens:
        return 0.0
    overlap = len(actual_tokens & predicted_tokens)
    precision = overlap / len(predicted_tokens)
    recall = overlap / len(actual_tokens)
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 3)


def _decision_alignment(
    actual: str,
    predicted: str,
    escalate: bool,
    similarity: float,
    *,
    actual_kind: str | None = None,
) -> float:
    actual_kind = actual_kind or _decision_kind(actual)
    predicted_kind = _decision_kind(predicted)
    if predicted_kind == "unknown" and escalate:
        predicted_kind = "verify"
    if actual_kind == "unknown" and predicted_kind == "unknown":
        return similarity
    if actual_kind == predicted_kind:
        return 1.0
    if {actual_kind, predicted_kind} <= {"choose", "proceed"}:
        return max(0.75, similarity)
    if actual_kind == "unknown" or predicted_kind == "unknown":
        return similarity
    return round(similarity * 0.5, 3)


def _row_decision_kind(row: dict[str, Any]) -> str:
    content_kind = _decision_kind(str(row.get("content") or ""))
    if content_kind != "unknown":
        return content_kind
    labels = set(row.get("labels") or [])
    if "choice-among-options" in labels:
        return "choose"
    if labels & {"verification-demand", "evidence-probe"}:
        return "verify"
    if labels & {"rejection", "interrupt-rollback", "scope-correction"}:
        return "reject"
    if "execute-short" in labels:
        return "proceed"
    return _decision_kind(str(row.get("content") or ""))


def _decision_kind(text: str) -> str:
    lowered = text.lower()
    stripped = lowered.strip()
    if any(
        x in lowered
        for x in (
            "pull",
            "rebase",
            "stash",
            "gh login",
            "gh auth",
            "auth login",
            "copy",
            "复制",
            "直接执行",
            "走起",
            "review一下",
            "已装",
            "清了",
            "加上",
        )
    ):
        return "proceed"
    if stripped.startswith(("好", "行", "嗯", "先", "直接", "按", "帮我", "查", "跑", "走", "用", "改", "补", "做", "搞")):
        return "proceed"
    if any(x in lowered for x in ("不确定", "确定吗", "确定你", "证据", "验证", "确认", "review", "看下log", "根本原因")):
        return "verify"
    if any(x in lowered for x in ("不行", "不对", "不要", "不用", "不是", "错了", "拒绝", "回退", "rollback")):
        return "reject"
    if any(x in lowered for x in ("选", "第二", "第一", "方式 1", "方式 2", "方案 a", "方案 b")):
        return "choose"
    if any(x in lowered for x in ("继续", "开始", "可以", "改吧", "修", "接受", "去github", "github上", "go", "proceed")):
        return "proceed"
    return "unknown"


def _render_report(
    cases: list[EvalCase],
    *,
    source_count: int,
    eligible_count: int,
    holdout_ratio: float,
    generate: bool,
) -> str:
    avg_align = _avg(case.decision_alignment for case in cases)
    avg_sim = _avg(case.similarity for case in cases)
    avg_conf = _avg(case.confidence for case in cases)
    lines = [
        f"# Keep Going Eval · {datetime.now().date().isoformat()}",
        "",
        "- eval_scope: lightweight_decision",
        f"- source_user_turns: {source_count}",
        f"- eligible_user_turns: {eligible_count}",
        f"- holdout_ratio: {holdout_ratio}",
        f"- evaluated_cases: {len(cases)}",
        f"- generated_mode: {str(generate).lower()}",
        f"- avg_decision_alignment: {avg_align:.3f}",
        f"- avg_text_similarity: {avg_sim:.3f}",
        f"- avg_confidence: {avg_conf:.3f}",
        "",
        "| turn_id | alignment | similarity | confidence | escalate | actual | predicted |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for case in cases:
        lines.append(
            "| {turn_id} | {align:.3f} | {sim:.3f} | {conf:.3f} | {esc} | {actual} | {pred} |".format(
                turn_id=case.turn_id,
                align=case.decision_alignment,
                sim=case.similarity,
                conf=case.confidence,
                esc=str(case.escalate).lower(),
                actual=_table_cell(_clip(case.actual, 120)),
                pred=_table_cell(_clip(case.predicted, 120)),
            )
        )
    return "\n".join(lines) + "\n"


def _avg(values: Any) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _table_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
