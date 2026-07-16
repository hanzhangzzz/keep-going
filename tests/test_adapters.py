"""Adapter smoke tests using synthetic JSONL fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from keep_going.corpus.adapters import claude_code as cc
from keep_going.corpus.adapters import codex as cx


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_claude_code_user_assistant_pairing(tmp_path: Path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "请实现 X"},
                "timestamp": "2026-05-01T03:00:00.000Z",
                "cwd": "/Users/USER/proj",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "好的，方案 A 或 B?"}]},
                "timestamp": "2026-05-01T03:00:05.000Z",
                "cwd": "/Users/USER/proj",
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "选 A"},
                "timestamp": "2026-05-01T03:00:10.000Z",
                "cwd": "/Users/USER/proj",
            },
        ],
    )
    turns = list(cc.iter_session(f))
    assert len(turns) == 3
    # Second user turn must have prev_assistant set
    user_turns = [t for t in turns if t.role == "user"]
    assert user_turns[1].prev_assistant == "好的，方案 A 或 B?"
    assert user_turns[0].prev_assistant is None
    assert all(t.source == "claude-code" for t in turns)


def test_claude_code_strips_command_envelope(tmp_path: Path):
    f = tmp_path / "session.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-message>foo</command-message><command-name>/foo</command-name><command-args>bar</command-args>\n真正的问题",
                },
                "timestamp": "2026-05-01T03:00:00.000Z",
            }
        ],
    )
    turns = list(cc.iter_session(f))
    assert len(turns) == 1
    assert turns[0].content.strip() == "真正的问题"


def test_codex_session_meta_then_messages(tmp_path: Path):
    f = tmp_path / "rollout-test.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "abc",
                    "timestamp": "2026-05-01T10:00:00.000Z",
                    "cwd": "/Users/USER/proj",
                },
            },
            {
                "timestamp": "2026-05-01T10:00:05.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "做个 X"}],
                },
            },
            {
                "timestamp": "2026-05-01T10:00:10.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "完成"}],
                },
            },
        ],
    )
    turns = list(cx.iter_session(f))
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].session_id == "abc"
    assert turns[0].project == "/Users/USER/proj"


def test_codex_skips_user_instructions_wrap(tmp_path: Path):
    f = tmp_path / "rollout-test.jsonl"
    _write_jsonl(
        f,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "abc",
                    "timestamp": "2026-05-01T10:00:00.000Z",
                    "cwd": "/x",
                },
            },
            {
                "timestamp": "2026-05-01T10:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<user_instructions>setup</user_instructions>"}
                    ],
                },
            },
            {
                "timestamp": "2026-05-01T10:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "actual question"}],
                },
            },
        ],
    )
    turns = list(cx.iter_session(f))
    assert len(turns) == 1
    assert turns[0].content == "actual question"


def test_harvest_filters_keep_going_stop_hook_injections():
    from keep_going.corpus.harvest import _is_envelope_noise

    claude_injection = (
        "Stop hook feedback:\n"
        "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：\n继续。\n\n"
        "请把上面内容当作用户回复继续处理；如果后续触及提交、推送等高风险动作，仍需真人确认。"
    )
    codex_injection = (
        '<hook_prompt hook_run_id="stop:12:/Users/USER/.codex/hooks.json">'
        "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：\n继续。</hook_prompt>"
    )
    assert _is_envelope_noise(claude_injection)
    assert _is_envelope_noise(codex_injection)
    assert not _is_envelope_noise("不对，这个方向错了，先停一下")
