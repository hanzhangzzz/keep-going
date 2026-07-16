from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import yaml

from keep_going import mcp_stdio
from keep_going.config import (
    Config,
    FiltersCfg,
    ModelsCfg,
    PathsCfg,
    ReasoningCfg,
    ScrubCfg,
    SourcesCfg,
    WindowCfg,
)
from keep_going.mcp_stdio import TRANSPORT_NDJSON, handle_request, read_framed_message, read_message, write_message
from keep_going.decision.policy_runtime import compile_runtime_policy


ROOT = Path(__file__).resolve().parents[1]


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


def _write_policy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 0.4,
                "core_principles": [{"id": "scope-fidelity", "statement": "只做当前要求的事。"}],
                "current_state_gates": [],
                "preferences": {},
                "redlines": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    compile_runtime_policy(path)


def _write_examples(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "turn_id": "turn1",
        "ts": "2026-05-01T00:00:00Z",
        "project": "/Users/USER/work/demo",
        "role": "user",
        "content": "继续，先验证再交付。",
        "prev_assistant": "要不要继续下一步？",
        "labels": ["execute-short", "verification-demand"],
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


def test_initialize_and_tools_list(tmp_path: Path):
    cfg = _config(tmp_path)

    init = handle_request(cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    tools = handle_request(cfg, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert init["result"]["serverInfo"]["name"] == "keep-going"
    assert {tool["name"] for tool in tools["result"]["tools"]} == {"keep_going_reply", "keep_going_eval"}


def test_tools_call_keep_going_reply(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")

    response = handle_request(
        cfg,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "keep_going_reply",
                "arguments": {"question": "要不要继续下一步？", "project": "/Users/USER/work/demo"},
            },
        },
    )

    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["reply"].startswith("继续")
    assert payload["model"] == "keep-going-model"


def test_tools_call_keep_going_eval_accepts_generate_flag(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    captured = {}

    def fake_run_eval(*args, **kwargs):
        captured["cfg"] = args[0]
        captured.update(kwargs)
        return tmp_path / "eval.md"

    monkeypatch.setattr(mcp_stdio, "run_eval", fake_run_eval)

    response = handle_request(
        cfg,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "keep_going_eval",
                "arguments": {
                    "holdout_ratio": 0.2,
                    "limit": 3,
                    "top_k": 2,
                    "out": str(tmp_path / "custom.md"),
                    "generate": True,
                },
            },
        },
    )

    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["report_path"] == str(tmp_path / "eval.md")
    assert captured["cfg"] == cfg
    assert captured["holdout_ratio"] == 0.2
    assert captured["limit"] == 3
    assert captured["top_k"] == 2
    assert captured["out_path"] == tmp_path / "custom.md"
    assert captured["generate"] is True


def test_stdio_content_length_round_trip():
    message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    stream = io.BytesIO()

    write_message(stream, message)
    stream.seek(0)

    assert read_message(stream) == message


def test_stdio_ndjson_round_trip():
    message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    stream = io.BytesIO()

    write_message(stream, message, transport=TRANSPORT_NDJSON)
    assert stream.getvalue().startswith(b'{"jsonrpc"')
    assert stream.getvalue().endswith(b"\n")

    stream.seek(0)
    framed = read_framed_message(stream)

    assert framed is not None
    assert framed.payload == message
    assert framed.transport == TRANSPORT_NDJSON


def test_repo_and_plugin_wrappers_serve_ndjson_tools_list():
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    wrappers = [
        ROOT / "scripts" / "04-mcp.sh",
        ROOT / "plugins" / "keep-going" / "scripts" / "mcp.sh",
    ]

    for wrapper in wrappers:
        result = subprocess.run(
            [str(wrapper)],
            cwd=ROOT,
            input=request,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert {tool["name"] for tool in response["result"]["tools"]} == {
            "keep_going_reply",
            "keep_going_eval",
        }
