"""Override audit: did the human later overturn a Keep Going stop-hook reply?

Scans real session transcripts for Keep Going stop-hook injections, finds the
next real human turn in the same session, and classifies whether that turn
overrides the Keep Going decision. Joins against the stop-hook event log to build
a confidence calibration table.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from keep_going.config import Config
from keep_going.corpus.adapters import claude_code as claude_adapter
from keep_going.corpus.adapters import codex as codex_adapter
from keep_going.corpus.classify import classify_text
from keep_going.corpus.harvest import _is_envelope_noise, _looks_like_command_only
from keep_going.corpus.schema import Turn
from keep_going.eval.loop_metrics import (
    _historical_session_roots,
    _project_matches,
    default_stop_hook_events_path,
)


INJECTION_MARKER = "Keep Going 已按项目级 Stop hook 代用户给出轻量决策"
INJECTION_TAIL = "请把上面内容当作用户回复继续处理"
HOOK_FEEDBACK_PREFIXES = ("stop hook feedback:", "<hook_prompt")
OVERRIDE_LABELS = frozenset({"rejection", "interrupt-rollback", "scope-correction"})
CONFIDENCE_BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))
EVENT_JOIN_TOLERANCE_SECONDS = 180.0
MAX_SAMPLES = 10


def run_override_audit(
    cfg: Config,
    *,
    events_path: Path | None = None,
    projects: tuple[str, ...] = (),
    window_days: int | None = None,
) -> dict[str, Any]:
    days = window_days or cfg.window.days
    since = datetime.now(timezone.utc) - timedelta(days=days)
    records = _collect_injections(cfg, since=since, projects=projects)
    events = _load_block_events(events_path or default_stop_hook_events_path(), projects=projects)
    join_stats = _join_confidence(records, events)
    return {
        "audit_version": 1,
        "window_days": days,
        "project_filters": list(projects),
        "events_path": str(events_path or default_stop_hook_events_path()),
        "summary": _summarize(records),
        "per_project": _per_project(records),
        "per_category": _per_category(records),
        "calibration": _calibration(records),
        "event_join": join_stats,
        "overridden_samples": _overridden_samples(records),
        "records": records,
    }


def _collect_injections(
    cfg: Config,
    *,
    since: datetime,
    projects: tuple[str, ...],
) -> list[dict[str, Any]]:
    sessions: dict[tuple[str, str], list[Turn]] = {}
    for turn in _iter_turns(cfg, since):
        sessions.setdefault((turn.source, turn.session_id), []).append(turn)
    records: list[dict[str, Any]] = []
    for turns in sessions.values():
        ordered = sorted(turns, key=lambda t: (t.ts, t.turn_idx))
        session_records = _audit_session(ordered)
        records.extend(
            r for r in session_records if _project_matches(r["project"], projects)
        )
    return sorted(records, key=lambda r: r["ts"])


def _iter_turns(cfg: Config, since: datetime) -> list[Turn]:
    turns = [
        turn
        for turn in claude_adapter.iter_sessions(cfg.sources.claude_code_dir, since=since)
        if turn.ts >= since
    ]
    for root in _historical_session_roots(cfg):
        turns.extend(turn for turn in codex_adapter.iter_sessions(root, since=since) if turn.ts >= since)
    return turns


def _audit_session(turns: list[Turn]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    last_assistant = ""
    for turn in turns:
        if turn.role == "assistant":
            last_assistant = turn.content
            continue
        if turn.role != "user":
            continue
        if INJECTION_MARKER in turn.content:
            record = _new_injection(turn, last_assistant)
            pending.append(record)
            records.append(record)
            continue
        if not _is_real_human_turn(turn.content):
            continue
        _resolve_pending(pending, turn)
        pending = []
    for i, record in enumerate(pending):
        record["resolution"] = "no_followup"
        record["attribution"] = "immediate" if i == len(pending) - 1 else "chain"
    return records


def _new_injection(turn: Turn, last_assistant: str) -> dict[str, Any]:
    return {
        "ts": turn.ts.isoformat(),
        "project": turn.project,
        "session_id": turn.session_id,
        "source": turn.source,
        "reply": _extract_reply(turn.content),
        "question_sha1": _sha1(last_assistant) if last_assistant else "",
        "resolution": "",
        "attribution": "",
        "next_human_excerpt": "",
        "next_human_labels": [],
        "gap_seconds": None,
        "confidence": None,
        "category": "",
        "join_method": "",
    }


def _resolve_pending(pending: list[dict[str, Any]], human_turn: Turn) -> None:
    if not pending:
        return
    labels = classify_text(human_turn.content)
    overridden = bool(set(labels) & OVERRIDE_LABELS)
    resolution = "overridden" if overridden else "sustained"
    for i, record in enumerate(pending):
        injected_at = datetime.fromisoformat(record["ts"])
        record["resolution"] = resolution
        record["attribution"] = "immediate" if i == len(pending) - 1 else "chain"
        record["next_human_excerpt"] = _clip(human_turn.content, 200)
        record["next_human_labels"] = labels
        record["gap_seconds"] = round((human_turn.ts - injected_at).total_seconds(), 1)


def _is_real_human_turn(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped or lowered in {"undefined", "null", "none"}:
        return False
    if any(lowered.startswith(prefix) for prefix in HOOK_FEEDBACK_PREFIXES):
        return False
    if _is_envelope_noise(stripped) or _looks_like_command_only(stripped):
        return False
    return True


def _extract_reply(text: str) -> str:
    start = text.find(INJECTION_MARKER)
    if start == -1:
        return ""
    body = text[start + len(INJECTION_MARKER) :].lstrip("：: \n")
    end = body.find(INJECTION_TAIL)
    if end != -1:
        body = body[:end]
    return body.strip()


def _load_block_events(path: Path, *, projects: tuple[str, ...]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("action") != "block" or not row.get("enabled"):
                continue
            if not _project_matches(str(row.get("project") or ""), projects):
                continue
            ts = _parse_ts(str(row.get("ts") or ""))
            if ts is None:
                continue
            events.append({**row, "_ts": ts, "_used": False})
    return sorted(events, key=lambda row: row["_ts"])


def _join_confidence(records: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    by_sha1: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        sha1 = str(event.get("question_sha1") or "")
        if sha1:
            by_sha1.setdefault(sha1, []).append(event)
    matched_sha1 = matched_ts = 0
    for record in records:
        event = _match_by_sha1(record, by_sha1) or _match_by_time(record, events)
        if event is None:
            continue
        event["_used"] = True
        record["confidence"] = event.get("confidence")
        record["category"] = event.get("category") or ""
        if record["join_method"] == "question_sha1":
            matched_sha1 += 1
        else:
            matched_ts += 1
    return {
        "block_events": len(events),
        "injections": len(records),
        "matched_by_sha1": matched_sha1,
        "matched_by_time": matched_ts,
        "unmatched": len(records) - matched_sha1 - matched_ts,
    }


def _match_by_sha1(record: dict[str, Any], by_sha1: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    for event in by_sha1.get(record["question_sha1"], []):
        if not event["_used"]:
            record["join_method"] = "question_sha1"
            return event
    return None


def _match_by_time(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    injected_at = datetime.fromisoformat(record["ts"])
    best: dict[str, Any] | None = None
    best_gap = EVENT_JOIN_TOLERANCE_SECONDS
    for event in events:
        if event["_used"] or str(event.get("project") or "") != record["project"]:
            continue
        gap = abs((injected_at - event["_ts"]).total_seconds())
        if gap <= best_gap:
            best, best_gap = event, gap
    if best is not None:
        record["join_method"] = "ts_window"
    return best


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    immediate = [r for r in records if r["attribution"] == "immediate"]
    resolved = [r for r in immediate if r["resolution"] in {"overridden", "sustained"}]
    overridden = [r for r in resolved if r["resolution"] == "overridden"]
    return {
        "injections_total": len(records),
        "immediate": len(immediate),
        "chain": len(records) - len(immediate),
        "resolved_with_human": len(resolved),
        "no_followup": sum(1 for r in immediate if r["resolution"] == "no_followup"),
        "overridden": len(overridden),
        "sustained": len(resolved) - len(overridden),
        "override_rate": round(len(overridden) / len(resolved), 4) if resolved else None,
    }


def _per_project(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for record in records:
        if record["attribution"] != "immediate":
            continue
        entry = stats.setdefault(
            record["project"], {"injections": 0, "overridden": 0, "sustained": 0, "no_followup": 0}
        )
        entry["injections"] += 1
        if record["resolution"] in entry:
            entry[record["resolution"]] += 1
    return dict(sorted(stats.items(), key=lambda kv: kv[1]["injections"], reverse=True))


def _per_category(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["attribution"] != "immediate":
            continue
        category = str(record.get("category") or "uncategorized")
        entry = stats.setdefault(
            category, {"injections": 0, "overridden": 0, "sustained": 0, "no_followup": 0, "override_rate": None}
        )
        entry["injections"] += 1
        if record["resolution"] in entry:
            entry[record["resolution"]] += 1
    for entry in stats.values():
        resolved = entry["overridden"] + entry["sustained"]
        entry["override_rate"] = round(entry["overridden"] / resolved, 4) if resolved else None
    return dict(sorted(stats.items(), key=lambda kv: kv[1]["injections"], reverse=True))


def _calibration(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scored = [
        r
        for r in records
        if r["attribution"] == "immediate"
        and isinstance(r.get("confidence"), (int, float))
        and r["resolution"] in {"overridden", "sustained"}
    ]
    for low, high in CONFIDENCE_BUCKETS:
        bucket = [r for r in scored if low <= float(r["confidence"]) < high]
        overridden = sum(1 for r in bucket if r["resolution"] == "overridden")
        rows.append(
            {
                "bucket": f"[{low:.1f}, {min(high, 1.0):.1f})",
                "n": len(bucket),
                "overridden": overridden,
                "override_rate": round(overridden / len(bucket), 4) if bucket else None,
            }
        )
    return rows


def _overridden_samples(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = [
        {
            "ts": r["ts"],
            "project": r["project"],
            "keep_going_reply": _clip(r["reply"], 200),
            "human_correction": r["next_human_excerpt"],
            "labels": r["next_human_labels"],
            "confidence": r.get("confidence"),
        }
        for r in records
        if r["resolution"] == "overridden" and r["attribution"] == "immediate"
    ]
    return samples[-MAX_SAMPLES:]


def render_override_audit(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Keep Going Override Audit",
        "",
        f"- window_days: {report['window_days']}",
        f"- events_path: `{report['events_path']}`",
        f"- project_filters: `{', '.join(report['project_filters']) or 'all'}`",
        "",
        "## Summary",
        "",
        f"- 代答注入总数: **{summary['injections_total']}**（immediate {summary['immediate']} / chain {summary['chain']}）",
        f"- 有后续真人发言的 immediate 代答: {summary['resolved_with_human']}",
        f"- 被推翻 (overridden): **{summary['overridden']}**",
        f"- 被延续 (sustained): {summary['sustained']}",
        f"- 无后续真人发言 (no_followup): {summary['no_followup']}",
        f"- **推翻率 (override rate): {_pct(summary['override_rate'])}**",
        "",
        "## Confidence Calibration",
        "",
        "| confidence bucket | n | overridden | override rate |",
        "|---|---:|---:|---:|",
    ]
    for row in report["calibration"]:
        lines.append(f"| {row['bucket']} | {row['n']} | {row['overridden']} | {_pct(row['override_rate'])} |")
    join = report["event_join"]
    lines.extend(
        [
            "",
            f"- 事件 join: sha1={join['matched_by_sha1']} / ts_window={join['matched_by_time']} / "
            f"unmatched={join['unmatched']}（block events {join['block_events']}）",
            "",
            "## Per Category（五类分诊 · 分级授权依据）",
            "",
            "| category | injections | overridden | sustained | no_followup | override rate |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for category, stats in report["per_category"].items():
        lines.append(
            f"| {category} | {stats['injections']} | {stats['overridden']} | {stats['sustained']} | "
            f"{stats['no_followup']} | {_pct(stats['override_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## Per Project",
            "",
            "| project | injections | overridden | sustained | no_followup |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for project, stats in report["per_project"].items():
        lines.append(
            f"| {project} | {stats['injections']} | {stats['overridden']} | {stats['sustained']} | {stats['no_followup']} |"
        )
    lines.extend(["", "## Overridden Samples", ""])
    if not report["overridden_samples"]:
        lines.append("- (none)")
    for sample in report["overridden_samples"]:
        lines.extend(
            [
                f"- {sample['ts']} `{sample['project']}` conf={sample['confidence']}",
                f"  - Keep Going: {sample['keep_going_reply']}",
                f"  - Human: {sample['human_correction']}",
            ]
        )
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- `immediate`: 真人发言前的最后一次代答；推翻只归因给它，同链更早的代答记为 `chain`。",
            "- `overridden`: 下一条真人发言命中 rejection / interrupt-rollback / scope-correction 标签。",
            "- `no_followup`: 代答后同 session 再无真人发言——通常意味着任务闭环，是最强的正面信号。",
            "- 校准表：如果高置信桶的推翻率不显著低于低置信桶，说明置信度是装饰品，升级门控形同虚设。",
            "",
        ]
    )
    return "\n".join(lines)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pct(rate: Any) -> str:
    if not isinstance(rate, (int, float)):
        return ""
    return f"{rate * 100:.1f}%"


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"
