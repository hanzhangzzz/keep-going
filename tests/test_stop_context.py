from __future__ import annotations

import json
from pathlib import Path

from keep_going.integration.stop_context import build_stop_decision_context


def _append_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _codex_message(role: str, text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    }


def test_stop_context_initializes_from_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-1", "cwd": str(tmp_path)}},
            _codex_message("user", "开始搞。只读分析，不要改文件。"),
            _codex_message("assistant", "我会先检查上下文。要不要继续？"),
        ],
    )

    context = build_stop_decision_context(
        {"transcript_path": str(transcript), "session_id": "sess-1"},
        project_path=tmp_path,
        cache_root=tmp_path / "cache",
    )

    assert context["context_status"] == "initialized"
    assert context["context_confidence"] == "high"
    assert context["latest_user_goal"] == "开始搞。只读分析，不要改文件。"
    assert "只读分析，不要改文件。" in context["explicit_constraints"]
    assert context["pending_question"] == "我会先检查上下文。要不要继续？"
    assert context["source"]["last_offset"] == transcript.stat().st_size
    assert (tmp_path / "cache" / "sess-1.json").exists()


def test_stop_context_rebuilds_incompatible_cache_schema(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "schema-cut", "cwd": str(tmp_path)}},
            _codex_message("user", "继续安全验证。"),
            _codex_message("assistant", "正在验证。"),
        ],
    )
    cache_root = tmp_path / "cache"
    initial = build_stop_decision_context(
        {"transcript_path": str(transcript), "session_id": "schema-cut"},
        project_path=tmp_path,
        cache_root=cache_root,
    )
    cache_path = Path(initial["source"]["cache_path"])
    stale = json.loads(cache_path.read_text(encoding="utf-8"))
    stale["schema_version"] = 1
    stale["decision_context"]["latest_user_goal"] = "stale cache must not survive"
    stale["decision_context"].pop("continuation_chain_depth")
    stale["decision_context"]["prior_chain_depth"] = 7
    cache_path.write_text(json.dumps(stale), encoding="utf-8")

    context = build_stop_decision_context(
        {"transcript_path": str(transcript), "session_id": "schema-cut"},
        project_path=tmp_path,
        cache_root=cache_root,
    )

    assert context["schema_version"] == 2
    assert context["latest_user_goal"] == "继续安全验证。"
    assert context["continuation_chain_depth"] == 7


def test_stop_context_flags_risk_from_assistant_question(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '\n'.join([
            '{"role":"user","content":"完成发布准备。"}',
            '{"role":"assistant","content":"要不要 git push --force origin main？"}',
        ]) + '\n',
        encoding="utf-8",
    )
    context = build_stop_decision_context(
        {"transcript_path": str(transcript), "session_id": "risk-session"},
        project_path=tmp_path,
        cache_root=tmp_path / "cache",
    )
    assert "mentions:push" in context["risk_flags"]


def test_stop_context_can_initialize_from_exact_session_id_without_transcript_path(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    transcript = codex_home / "sessions" / "2026" / "06" / "01" / "rollout-2026-06-01T00-00-00-sess-lookup.jsonl"
    transcript.parent.mkdir(parents=True)
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-lookup", "cwd": str(tmp_path)}},
            _codex_message("user", "只看当前 session，不要全局猜测。"),
            _codex_message("assistant", "已定位上下文。"),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    context = build_stop_decision_context(
        {"session_id": "sess-lookup"},
        project_path=tmp_path,
        cache_root=tmp_path / "cache",
    )

    assert context["context_status"] == "initialized"
    assert context["latest_user_goal"] == "只看当前 session，不要全局猜测。"
    assert context["source"]["transcript_path"] == str(transcript)
    assert context["source"]["transcript_resolution"] == "session_id_filename"


def test_stop_context_uses_incremental_updates_after_initialization(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    cache_root = tmp_path / "cache"
    event = {"transcript_path": str(transcript), "session_id": "sess-1"}
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-1", "cwd": str(tmp_path)}},
            _codex_message("user", "第一轮目标：只计划，不执行。"),
            _codex_message("assistant", "已整理计划。"),
        ],
    )
    first = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)
    first_offset = first["source"]["last_offset"]

    _append_jsonl(
        transcript,
        [
            _codex_message("user", "第二轮：开始实现，但不要提交。"),
            _codex_message("assistant", "已经改完。要不要跑验证？"),
        ],
    )
    second = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)

    assert second["context_status"] == "incremental"
    assert second["latest_user_goal"] == "第二轮：开始实现，但不要提交。"
    assert second["source"]["read_from_offset"] == first_offset
    assert second["source"]["new_lines"] == 2
    user_turns = [turn for turn in second["recent_turns"] if turn["role"] == "user"]
    assert [turn["text"] for turn in user_turns] == [
        "第一轮目标：只计划，不执行。",
        "第二轮：开始实现，但不要提交。",
    ]


def test_stop_context_reinitializes_when_transcript_is_rewritten_past_old_offset(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    cache_root = tmp_path / "cache"
    event = {"transcript_path": str(transcript), "session_id": "sess-rewrite"}
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-rewrite", "cwd": str(tmp_path)}},
            _codex_message("user", "旧目标：先计划。"),
            _codex_message("assistant", "旧上下文。"),
        ],
    )
    first = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)

    transcript.write_text("", encoding="utf-8")
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-rewrite", "cwd": str(tmp_path)}},
            _codex_message("user", "新目标：重写 transcript 后必须重新初始化。"),
            _codex_message("assistant", "新上下文。" + ("扩展内容" * 80)),
        ],
    )
    second = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)

    assert transcript.stat().st_size > first["source"]["last_offset"]
    assert second["context_status"] == "initialized"
    assert second["latest_user_goal"] == "新目标：重写 transcript 后必须重新初始化。"
    assert second["source"]["read_from_offset"] == 0


def test_stop_context_shrinks_to_byte_budget(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    long_text = "需要保留最新目标，但是历史内容很长。" + ("填充内容" * 300)
    rows: list[dict[str, object]] = [{"type": "session_meta", "payload": {"id": "sess-big", "cwd": str(tmp_path)}}]
    for index in range(20):
        rows.append(_codex_message("user", f"第 {index} 轮：{long_text}"))
        rows.append(_codex_message("assistant", f"第 {index} 轮助手输出。" + ("输出内容" * 300)))
    rows.append(_codex_message("user", "最终目标：继续实现，必须先验证，不要提交。"))
    _append_jsonl(transcript, rows)

    context = build_stop_decision_context(
        {"transcript_path": str(transcript), "session_id": "sess-big"},
        project_path=tmp_path,
        cache_root=tmp_path / "cache",
        max_context_bytes=2200,
    )
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")

    assert len(encoded) <= 2200
    assert context["latest_user_goal"] == "最终目标：继续实现，必须先验证，不要提交。"
    assert "必须先验证，不要提交。" in context["explicit_constraints"]
    assert len(context["recent_turns"]) < 40


def test_stop_context_drops_system_envelope_user_turns(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-env", "cwd": str(tmp_path)}},
            _codex_message("user", "真实目标：实现登录限流。"),
            _codex_message("assistant", "已实现限流。"),
            _codex_message("user", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
            _codex_message("user", "<system-reminder>some injected context</system-reminder>"),
        ],
    )

    context = build_stop_decision_context(
        {"transcript_path": str(transcript), "session_id": "sess-env"},
        project_path=tmp_path,
        cache_root=tmp_path / "cache",
    )

    # System-injected envelopes must never be mistaken for the user's goal.
    assert context["latest_user_goal"] == "真实目标：实现登录限流。"
    assert all("task-notification" not in str(turn.get("text", "")) for turn in context["recent_turns"])


def test_stop_context_tracks_continuation_chain_depth_across_injections(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    cache_root = tmp_path / "cache"
    event = {"transcript_path": str(transcript), "session_id": "sess-depth"}
    _append_jsonl(
        transcript,
        [
            {"type": "session_meta", "payload": {"id": "sess-depth", "cwd": str(tmp_path)}},
            _codex_message("user", "目标：逐题确认设计。"),
            _codex_message("assistant", "问题1：用方案A还是B？"),
        ],
    )
    first = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)
    assert first["continuation_chain_depth"] == 0

    _append_jsonl(
        transcript,
        [
            _codex_message(
                "user",
                "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：选方案A。\n请把上面内容当作用户回复继续处理；",
            ),
            _codex_message("assistant", "问题2：要不要加缓存？"),
        ],
    )
    second = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)
    assert second["continuation_chain_depth"] == 1

    _append_jsonl(
        transcript,
        [
            _codex_message("user", "真人重新介入：先停一下，换个方向。"),
            _codex_message("assistant", "好的，按新方向。"),
        ],
    )
    third = build_stop_decision_context(event, project_path=tmp_path, cache_root=cache_root)
    # A genuine human turn resets the chain so the depth cap restarts fresh.
    assert third["continuation_chain_depth"] == 0
