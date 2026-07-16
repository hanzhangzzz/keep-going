from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from keep_going import cli
from keep_going.config import Config, FiltersCfg, ModelsCfg, PathsCfg, ReasoningCfg, ScrubCfg, SourcesCfg, WindowCfg
from keep_going.eval.overrides import _extract_reply, render_override_audit, run_override_audit


INJECTION_TEMPLATE = (
    "Stop hook feedback:\n"
    "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：\n"
    "{reply}\n\n"
    "请把上面内容当作用户回复继续处理；如果后续触及提交、推送、删除、生产或密钥等高风险动作，仍需真人确认。"
)


def _config(root: Path) -> Config:
    return Config(
        window=WindowCfg(days=90),
        sources=SourcesCfg(
            claude_code_dir=root / "claude",
            codex_archived_dir=root / "codex-archived",
            codex_history=root / "history.jsonl",
            codex_sessions_dir=root / "codex-sessions",
        ),
        paths=PathsCfg(data_dir=root / "data", artifacts_dir=root / "artifacts"),
        scrub=ScrubCfg(enabled=True, user_replacement="USER"),
        models=ModelsCfg(reasoning="reasoning", eval="eval", decision="keep-going-model"),
        reasoning=ReasoningCfg(max_content_chars=4000, max_prev_assistant_chars=2000, concurrency=1),
        filters=FiltersCfg(min_user_chars=4, skip_command_only=True),
        root=root,
    )


def _claude_row(role: str, text: str, ts: datetime, project: str) -> dict[str, object]:
    return {
        "type": role,
        "message": {"role": role, "content": text},
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "cwd": project,
    }


def _write_session(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _build_transcripts(root: Path, project: str) -> dict[str, datetime]:
    now = datetime.now(timezone.utc)
    t = {f"t{i}": now - timedelta(minutes=60 - i * 5) for i in range(10)}
    claude = root / "claude" / "proj"
    _write_session(
        claude / "session-override.jsonl",
        [
            _claude_row("assistant", "方案 A 还是 B？要不要继续？", t["t0"], project),
            _claude_row("user", INJECTION_TEMPLATE.format(reply="选 A，按最小改动推进。"), t["t1"], project),
            _claude_row("assistant", "已按 A 完成初版。要不要继续？", t["t2"], project),
            _claude_row("user", INJECTION_TEMPLATE.format(reply="继续。先保证产物级验证。"), t["t3"], project),
            _claude_row("user", "不对，这个方向错了，先停一下", t["t4"], project),
        ],
    )
    _write_session(
        claude / "session-sustained.jsonl",
        [
            _claude_row("assistant", "文档已更新，要不要继续下一节？", t["t0"], project),
            _claude_row("user", INJECTION_TEMPLATE.format(reply="继续下一节。"), t["t1"], project),
            _claude_row("user", "好的，继续吧", t["t2"], project),
        ],
    )
    _write_session(
        claude / "session-no-followup.jsonl",
        [
            _claude_row("assistant", "全部测试通过，要不要收尾？", t["t0"], project),
            _claude_row("user", INJECTION_TEMPLATE.format(reply="收尾。列验证结果。"), t["t1"], project),
        ],
    )
    return t


def test_override_audit_classifies_resolutions(tmp_path: Path):
    project = "/work/demo"
    times = _build_transcripts(tmp_path, project)
    events = tmp_path / "stop-hook.jsonl"
    question_sha1 = hashlib.sha1("已按 A 完成初版。要不要继续？".encode("utf-8")).hexdigest()
    events.write_text(
        json.dumps(
            {
                "ts": times["t3"].isoformat(),
                "project": project,
                "enabled": True,
                "action": "block",
                "confidence": 0.86,
                "category": "preference",
                "question_sha1": question_sha1,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_override_audit(_config(tmp_path), events_path=events)

    summary = report["summary"]
    assert summary["injections_total"] == 4
    assert summary["immediate"] == 3
    assert summary["chain"] == 1
    assert summary["resolved_with_human"] == 2
    assert summary["overridden"] == 1
    assert summary["sustained"] == 1
    assert summary["no_followup"] == 1
    assert summary["override_rate"] == 0.5

    overridden = [r for r in report["records"] if r["resolution"] == "overridden" and r["attribution"] == "immediate"]
    assert len(overridden) == 1
    assert overridden[0]["reply"] == "继续。先保证产物级验证。"
    assert overridden[0]["confidence"] == 0.86
    assert overridden[0]["category"] == "preference"
    assert overridden[0]["join_method"] == "question_sha1"

    calibration = {row["bucket"]: row for row in report["calibration"]}
    assert calibration["[0.8, 0.9)"]["n"] == 1
    assert calibration["[0.8, 0.9)"]["override_rate"] == 1.0

    per_category = report["per_category"]
    assert per_category["preference"]["injections"] == 1
    assert per_category["preference"]["overridden"] == 1
    assert per_category["preference"]["override_rate"] == 1.0
    assert per_category["uncategorized"]["injections"] == 2

    rendered = render_override_audit(report)
    assert "推翻率" in rendered
    assert "50.0%" in rendered


def test_override_audit_time_window_join(tmp_path: Path):
    project = "/work/demo"
    times = _build_transcripts(tmp_path, project)
    events = tmp_path / "stop-hook.jsonl"
    events.write_text(
        json.dumps(
            {
                "ts": (times["t1"] + timedelta(seconds=30)).isoformat(),
                "project": project,
                "enabled": True,
                "action": "block",
                "confidence": 0.7,
                "question_sha1": "no-match",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_override_audit(_config(tmp_path), events_path=events)

    assert report["event_join"]["matched_by_time"] == 1
    joined = [r for r in report["records"] if r["join_method"] == "ts_window"]
    assert len(joined) == 1
    assert joined[0]["confidence"] == 0.7


def test_extract_reply_strips_marker_and_tail():
    text = INJECTION_TEMPLATE.format(reply="选 A。理由：最小改动。")
    assert _extract_reply(text) == "选 A。理由：最小改动。"
    assert _extract_reply("没有标记的普通文本") == ""


def test_overrides_cli_json_output(monkeypatch, tmp_path: Path):
    project = "/work/demo"
    _build_transcripts(tmp_path, project)
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    result = CliRunner().invoke(
        cli.main,
        ["overrides", "--events", str(tmp_path / "missing-events.jsonl"), "--json-output"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["audit_version"] == 1
    assert payload["summary"]["injections_total"] == 4
    assert payload["event_join"]["block_events"] == 0
