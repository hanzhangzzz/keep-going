from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from keep_going import cli
from keep_going.config import Config, FiltersCfg, ModelsCfg, PathsCfg, ReasoningCfg, ScrubCfg, SourcesCfg, WindowCfg
from keep_going.eval.loop_metrics import render_loop_metrics, run_loop_metrics


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


def _config_with_codex_sessions(root: Path, codex_sessions: Path) -> Config:
    base = _config(root)
    return Config(
        window=base.window,
        sources=SourcesCfg(
            claude_code_dir=base.sources.claude_code_dir,
            codex_archived_dir=base.sources.codex_archived_dir,
            codex_history=base.sources.codex_history,
            codex_sessions_dir=codex_sessions,
        ),
        paths=base.paths,
        scrub=base.scrub,
        models=base.models,
        reasoning=base.reasoning,
        filters=base.filters,
        root=base.root,
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_loop_metrics_computes_human_interval_and_automation_substitution(tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    events = tmp_path / "stop-hook.jsonl"
    project = "/work/demo"
    _write_jsonl(
        turns,
        [
            {
                "source": "codex",
                "session_id": "s1",
                "ts": "2026-05-01T00:10:00Z",
                "project": project,
                "role": "user",
                "content": "继续",
                "prev_assistant": "要不要继续最终验证？",
            },
            {
                "source": "codex",
                "session_id": "s1",
                "ts": "2026-05-01T00:30:00Z",
                "project": project,
                "role": "user",
                "content": "只改当前文件",
                "prev_assistant": "是否扩大范围？",
            },
            {
                "source": "codex",
                "session_id": "s1",
                "ts": "2026-05-01T01:00:00Z",
                "project": project,
                "role": "user",
                "content": "帮我生成一个新报告",
                "prev_assistant": "已经完成了。",
            },
        ],
    )
    _write_jsonl(
        events,
        [
            {
                "ts": "2026-05-01T00:40:00Z",
                "project": project,
                "project_id": "demo-1",
                "host": "codex",
                "hook_event_name": "Stop",
                "action": "block",
                "continuation_injected": True,
                "escalate": False,
            },
            {
                "ts": "2026-05-01T00:45:00Z",
                "project": project,
                "project_id": "demo-1",
                "host": "codex",
                "action": "block",
                "continuation_injected": True,
                "escalate": False,
            }
        ],
    )

    report = run_loop_metrics(_config(tmp_path), turns_path=turns, events_path=events)

    summary = report["periods"]["all"]
    assert summary["human_interventions"] == 3
    assert summary["loop_blocking_human_interventions"] == 2
    assert summary["human_interval_count"] == 2
    assert summary["human_interval_mean_seconds"] == 1500.0
    assert summary["continuation_injected_interventions"] == 1
    assert summary["automation_substitution_rate"] == 1 / 3
    rendered = render_loop_metrics(report)
    assert "MTBHI" in rendered
    assert "33.3%" in rendered


def test_loop_metrics_cli_json_output(monkeypatch, tmp_path: Path):
    turns = tmp_path / "turns.jsonl"
    _write_jsonl(
        turns,
        [
            {
                "source": "codex",
                "session_id": "s1",
                "ts": "2026-05-01T00:10:00Z",
                "project": "/work/demo",
                "role": "user",
                "content": "继续",
                "prev_assistant": "要不要继续？",
            }
        ],
    )
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    result = CliRunner().invoke(cli.main, ["loop-metrics", "--turns", str(turns), "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["metric_version"] == 1
    assert payload["human_source"] == "turns-jsonl"
    assert payload["periods"]["all"]["human_interventions"] == 1


def test_loop_metrics_reads_raw_codex_session_logs_by_default(tmp_path: Path):
    codex_sessions = tmp_path / "codex-sessions"
    session = codex_sessions / "2026" / "05" / "26" / "rollout-2026-05-26T10-00-00-demo.jsonl"
    now = datetime.now(timezone.utc)
    rows = [
        {
            "timestamp": (now - timedelta(minutes=40)).isoformat().replace("+00:00", "Z"),
            "type": "session_meta",
            "payload": {"id": "demo-session", "cwd": "/Users/USER/Desktop/Works/demo-wiki"},
        },
        {
            "timestamp": (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "要不要继续最终验证？"}],
            },
        },
        {
            "timestamp": (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "继续"}],
            },
        },
        {
            "timestamp": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "是否扩大范围？"}],
            },
        },
        {
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "只改当前项目"}],
            },
        },
    ]
    _write_jsonl(session, rows)

    report = run_loop_metrics(
        _config_with_codex_sessions(tmp_path, codex_sessions),
        events_path=tmp_path / "missing-events.jsonl",
        projects=("/Users/USER/Desktop/Works/demo-wiki",),
    )

    assert report["human_source"] == "raw-session-logs"
    assert str(codex_sessions) in report["human_source_paths"]
    summary = report["periods"]["all"]
    assert summary["human_interventions"] == 2
    assert summary["loop_blocking_human_interventions"] == 2
    assert summary["human_interval_mean_seconds"] == 1200.0
