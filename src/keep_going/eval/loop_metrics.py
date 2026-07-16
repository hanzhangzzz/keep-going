"""Agent-loop intervention metrics for Keep Going adoption tracking."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from keep_going.config import Config
from keep_going.corpus.adapters import claude_code as claude_adapter
from keep_going.corpus.adapters import codex as codex_adapter
from keep_going.corpus.classify import classify_text
from keep_going.corpus.harvest import _is_envelope_noise, _looks_like_command_only
from keep_going.corpus.schema import Turn


DECISION_LABELS = {
    "choice-among-options",
    "execute-short",
    "verification-demand",
    "evidence-probe",
    "scope-correction",
    "interrupt-rollback",
    "rejection",
}
NON_BLOCKING_LABELS = {"task-kickoff", "delivery-finalize", "context-statement"}
DECISION_PROMPT_NEEDLES = (
    "要不要",
    "是否",
    "继续",
    "下一步",
    "选",
    "方案",
    "哪种",
    "确认",
    "可以吗",
    "要我",
    "proceed",
    "option",
    "plan",
)


@dataclass(frozen=True)
class InterventionEvent:
    ts: datetime
    project: str
    session_id: str
    source: str
    kind: str
    period: str


def default_stop_hook_events_path() -> Path:
    return Path.home() / ".keep-going" / "events" / "stop-hook.jsonl"


def run_loop_metrics(
    cfg: Config,
    *,
    turns_path: Path | None = None,
    events_path: Path | None = None,
    projects: tuple[str, ...] = (),
    split_at: datetime | None = None,
) -> dict[str, Any]:
    events = events_path or default_stop_hook_events_path()
    filters = tuple(projects)
    if turns_path is None:
        human_source = "raw-session-logs"
        human_source_paths = [str(path) for path in _historical_session_roots(cfg)]
        human_events = _load_human_interventions_from_sources(cfg, projects=filters, split_at=split_at)
    else:
        human_source = "turns-jsonl"
        human_source_paths = [str(turns_path)]
        human_events = _load_human_interventions(turns_path, projects=filters, split_at=split_at)
    keep_going_events = _load_keep_going_events(events, projects=filters, split_at=split_at)
    periods = ("all", "before", "after") if split_at is not None else ("all",)
    report = {
        "metric_version": 1,
        "human_source": human_source,
        "human_source_paths": human_source_paths,
        "events_path": str(events),
        "project_filters": list(filters),
        "split_at": split_at.isoformat() if split_at else None,
        "periods": {},
        "projects": {},
    }
    for period in periods:
        period_human = _period_events(human_events, period)
        period_keep_going = _period_events(keep_going_events, period)
        raw = [event for event in period_human if event.kind == "human"]
        blocking = [event for event in period_human if event.kind == "human_loop_blocking"]
        continuation_injected = [event for event in period_keep_going if event.kind == "continuation_injected"]
        escalated = [event for event in period_keep_going if event.kind == "decision_escalated"]
        report["periods"][period] = _period_summary(raw, blocking, continuation_injected, escalated)

    for project in sorted({event.project for event in human_events + keep_going_events}):
        raw = [event for event in human_events if event.project == project and event.kind == "human"]
        blocking = [event for event in human_events if event.project == project and event.kind == "human_loop_blocking"]
        continuation_injected = [event for event in keep_going_events if event.project == project and event.kind == "continuation_injected"]
        escalated = [event for event in keep_going_events if event.project == project and event.kind == "decision_escalated"]
        report["projects"][project] = _period_summary(raw, blocking, continuation_injected, escalated)
    return report


def render_loop_metrics(report: dict[str, Any]) -> str:
    lines = [
        "# Keep Going Agent Loop Metrics",
        "",
        f"- metric_version: {report['metric_version']}",
        f"- human_source: `{report['human_source']}`",
        f"- human_source_paths: `{', '.join(report['human_source_paths'])}`",
        f"- events_path: `{report['events_path']}`",
        f"- split_at: `{report['split_at'] or ''}`",
        f"- project_filters: `{', '.join(report['project_filters']) or 'all'}`",
        "",
        "## Period Summary",
        "",
        "| period | human turns | loop-blocking human turns | MTBHI mean | MTBHI median | keep-going-handled | keep-going escalated | substitution rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for period, data in report["periods"].items():
        lines.append(
            "| {period} | {human} | {blocking} | {mean} | {median} | {keep_going} | {escalated} | {rate} |".format(
                period=period,
                human=data["human_interventions"],
                blocking=data["loop_blocking_human_interventions"],
                mean=_minutes_cell(data["human_interval_mean_seconds"]),
                median=_minutes_cell(data["human_interval_median_seconds"]),
                keep_going=data["continuation_injected_interventions"],
                escalated=data["decision_escalated_interventions"],
                rate=_rate_cell(data["automation_substitution_rate"]),
            )
        )
    if report["projects"]:
        lines.extend(
            [
                "",
                "## Project Summary",
                "",
                "| project | human turns | loop-blocking human turns | MTBHI mean | keep-going-handled | substitution rate |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for project, data in report["projects"].items():
            lines.append(
                "| {project} | {human} | {blocking} | {mean} | {keep_going} | {rate} |".format(
                    project=_cell(project),
                    human=data["human_interventions"],
                    blocking=data["loop_blocking_human_interventions"],
                    mean=_minutes_cell(data["human_interval_mean_seconds"]),
                    keep_going=data["continuation_injected_interventions"],
                    rate=_rate_cell(data["automation_substitution_rate"]),
                )
            )
    lines.extend(
        [
            "",
            "## Definitions",
            "",
            "- `human turns`: real user turns that respond to a previous assistant turn inside the same session.",
            "- `loop-blocking human turns`: the subset that looks like a lightweight decision, correction, verification demand, rejection, rollback, or continue/choose response.",
            "- `MTBHI`: mean time between human interventions, computed within each session from consecutive real user turns; larger is better for agent-loop autonomy.",
            "- `keep-going-handled`: Stop hook events where Keep Going injected a non-escalated reply and allowed the agent to continue without a real user turn.",
            "- `substitution rate`: `keep-going-handled / (keep-going-handled + loop-blocking human turns)` for the same period or project.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_human_interventions(
    turns_path: Path,
    *,
    projects: tuple[str, ...],
    split_at: datetime | None,
) -> list[InterventionEvent]:
    if not turns_path.exists():
        raise FileNotFoundError(f"missing turns file: {turns_path}")
    events: list[InterventionEvent] = []
    with turns_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("role") != "user" or not row.get("prev_assistant"):
                continue
            project = str(row.get("project") or "")
            if not _project_matches(project, projects):
                continue
            content = str(row.get("content") or "")
            if _is_noise(content):
                continue
            ts = _parse_ts(str(row.get("ts") or ""))
            if ts is None:
                continue
            period = _period(ts, split_at)
            source = str(row.get("source") or "")
            session_id = str(row.get("session_id") or "")
            events.append(InterventionEvent(ts, project, session_id, source, "human", period))
            if _is_loop_blocking_turn(content, str(row.get("prev_assistant") or "")):
                events.append(InterventionEvent(ts, project, session_id, source, "human_loop_blocking", period))
    return sorted(events, key=lambda event: (event.ts, event.source, event.session_id, event.kind))


def _load_human_interventions_from_sources(
    cfg: Config,
    *,
    projects: tuple[str, ...],
    split_at: datetime | None,
) -> list[InterventionEvent]:
    events: list[InterventionEvent] = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for turn in _iter_historical_turns(cfg):
        if turn.role != "user" or not turn.prev_assistant:
            continue
        if not _project_matches(turn.project, projects):
            continue
        if _is_noise(turn.content) or _is_envelope_noise(turn.content) or _looks_like_command_only(turn.content):
            continue
        key = (turn.source, turn.session_id, turn.turn_idx, turn.ts.isoformat(), turn.content)
        if key in seen:
            continue
        seen.add(key)
        events.extend(_human_events_from_turn(turn, split_at=split_at))
    return sorted(events, key=lambda event: (event.ts, event.source, event.session_id, event.kind))


def _iter_historical_turns(cfg: Config) -> list[Turn]:
    roots = _historical_session_roots(cfg)
    since = datetime.now(timezone.utc) - timedelta(days=cfg.window.days)
    turns: list[Turn] = []
    turns.extend(turn for turn in claude_adapter.iter_sessions(cfg.sources.claude_code_dir, since=since) if turn.ts >= since)
    for root in roots:
        turns.extend(turn for turn in codex_adapter.iter_sessions(root, since=since) if turn.ts >= since)
    return turns


def _historical_session_roots(cfg: Config) -> list[Path]:
    roots = [cfg.sources.codex_archived_dir]
    sessions = cfg.sources.codex_sessions_dir or (Path.home() / ".codex" / "sessions")
    if sessions not in roots:
        roots.append(sessions)
    return roots


def _human_events_from_turn(turn: Turn, *, split_at: datetime | None) -> list[InterventionEvent]:
    events = [
        InterventionEvent(
            ts=turn.ts,
            project=turn.project,
            session_id=turn.session_id,
            source=turn.source,
            kind="human",
            period=_period(turn.ts, split_at),
        )
    ]
    if _is_loop_blocking_turn(turn.content, turn.prev_assistant or ""):
        events.append(
            InterventionEvent(
                ts=turn.ts,
                project=turn.project,
                session_id=turn.session_id,
                source=turn.source,
                kind="human_loop_blocking",
                period=_period(turn.ts, split_at),
            )
        )
    return events


def _load_keep_going_events(
    events_path: Path,
    *,
    projects: tuple[str, ...],
    split_at: datetime | None,
) -> list[InterventionEvent]:
    if not events_path.exists():
        return []
    events: list[InterventionEvent] = []
    with events_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            project = str(row.get("project") or "")
            if not _project_matches(project, projects):
                continue
            if str(row.get("hook_event_name") or "") != "Stop":
                continue
            ts = _parse_ts(str(row.get("ts") or ""))
            if ts is None:
                continue
            kind = ""
            if row.get("action") == "block" and row.get("continuation_injected") is True and row.get("escalate") is False:
                kind = "continuation_injected"
            elif row.get("reason") == "keep-going escalated to human" or row.get("escalate") is True:
                kind = "decision_escalated"
            if not kind:
                continue
            events.append(
                InterventionEvent(
                    ts=ts,
                    project=project,
                    session_id=str(row.get("project_id") or ""),
                    source=str(row.get("host") or "stop-hook"),
                    kind=kind,
                    period=_period(ts, split_at),
                )
            )
    return sorted(events, key=lambda event: (event.ts, event.source, event.session_id, event.kind))


def _period_summary(
    human: list[InterventionEvent],
    blocking: list[InterventionEvent],
    continuation_injected: list[InterventionEvent],
    escalated: list[InterventionEvent],
) -> dict[str, Any]:
    intervals = _intervals(human)
    denominator = len(blocking) + len(continuation_injected)
    return {
        "human_interventions": len(human),
        "loop_blocking_human_interventions": len(blocking),
        "human_interval_count": len(intervals),
        "human_interval_mean_seconds": _mean(intervals),
        "human_interval_median_seconds": _median(intervals),
        "human_interval_p75_seconds": _quantile(intervals, 0.75),
        "human_interval_p90_seconds": _quantile(intervals, 0.90),
        "continuation_injected_interventions": len(continuation_injected),
        "decision_escalated_interventions": len(escalated),
        "automation_substitution_rate": len(continuation_injected) / denominator if denominator else None,
    }


def _intervals(events: list[InterventionEvent]) -> list[float]:
    grouped: dict[tuple[str, str, str], list[InterventionEvent]] = defaultdict(list)
    for event in events:
        grouped[(event.source, event.session_id, event.project)].append(event)
    intervals: list[float] = []
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda event: event.ts)
        for prev, current in zip(ordered, ordered[1:]):
            delta = (current.ts - prev.ts).total_seconds()
            if delta > 0:
                intervals.append(delta)
    return intervals


def _is_loop_blocking_turn(content: str, prev_assistant: str) -> bool:
    labels = set(classify_text(content))
    if labels & DECISION_LABELS:
        return True
    if labels & NON_BLOCKING_LABELS:
        return False
    lowered_prompt = prev_assistant.lower()
    return len(content.strip()) <= 160 and any(needle.lower() in lowered_prompt for needle in DECISION_PROMPT_NEEDLES)


def _period_events(events: list[InterventionEvent], period: str) -> list[InterventionEvent]:
    if period == "all":
        return events
    return [event for event in events if event.period == period]


def _period(ts: datetime, split_at: datetime | None) -> str:
    if split_at is None:
        return "all"
    return "before" if ts < split_at else "after"


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _project_matches(project: str, filters: tuple[str, ...]) -> bool:
    if not filters:
        return True
    normalized = project.rstrip("/")
    return any(normalized == item.rstrip("/") or normalized.endswith("/" + item.strip("/")) for item in filters)


def _is_noise(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    return lowered in {"undefined", "null", "none"} or lowered.startswith("stop hook feedback:")


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def _minutes_cell(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds / 60:.1f}m"


def _rate_cell(rate: float | None) -> str:
    if rate is None:
        return ""
    return f"{rate * 100:.1f}%"


def _cell(value: str) -> str:
    return value.replace("|", "\\|")
