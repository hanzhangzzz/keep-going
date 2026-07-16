from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from keep_going import cli
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
from keep_going.decision.hook import handle_hook_event, parse_hook_event
from keep_going.decision.policy_runtime import compile_runtime_policy


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
                "core_principles": [
                    {"id": "evidence-first", "statement": "结论要带证据。"},
                    {"id": "scope-fidelity", "statement": "只做当前要求的事。"},
                ],
                "current_state_gates": [
                    {"id": "git-write-needs-authorization", "gate": "不主动 commit / push。"}
                ],
                "preferences": {},
                "redlines": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


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


def _write_fixture(cfg: Config) -> None:
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    _write_policy(source)
    compile_runtime_policy(source)
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")


def test_parse_hook_event_rejects_non_object_json():
    try:
        parse_hook_event("[]")
    except ValueError as exc:
        assert "must be an object" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_handle_hook_event_asks_keep_going_for_explicit_question(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)

    result = handle_hook_event(cfg, {"question": "要不要继续下一步？", "project": "/Users/USER/work/demo"})

    assert result["continuation_injected"] is True
    assert result["reply"].startswith("继续")
    assert result["hook_reason"] == "explicit_question"


def test_handle_hook_event_noops_without_decision_signal(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)

    result = handle_hook_event(cfg, {"prompt": "帮我写一个普通函数", "project": "/Users/USER/work/demo"})

    assert result["continuation_injected"] is False
    assert result["reply"] == ""


def test_handle_hook_event_escalates_dangerous_tool_use(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)

    result = handle_hook_event(
        cfg,
        {"hook_event_name": "PreToolUse", "tool_name": "bash", "tool_input": {"command": "git push origin main"}},
    )

    assert result["continuation_injected"] is True
    assert result["escalate"] is True
    assert result["reply"].startswith("这个需要真人确认")


def test_hook_cli_accepts_input_json(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(
        cli.main,
        ["hook", "--input-json"],
        input=json.dumps({"question": "要不要继续下一步？", "project": "/Users/USER/work/demo"}),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["continuation_injected"] is True
    assert payload["reply"].startswith("继续")


def test_handle_hook_event_uses_canonical_policy_by_default(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)

    result = handle_hook_event(cfg, {"question": "要不要继续下一步？", "project": "/Users/USER/work/demo"})

    assert result["continuation_injected"] is True
    assert result["reply"]


def test_handle_hook_event_uses_custom_policy_path(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)

    alt_policy = tmp_path / "alt-decision-policy.yaml"
    alt_policy.parent.mkdir(parents=True, exist_ok=True)
    alt_policy.write_text(
        yaml.safe_dump(
            {
                "core_principles": [{"id": "alt-principle", "statement": "替代原则。"}],
                "current_state_gates": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    result = handle_hook_event(
        cfg,
        {"question": "要不要继续下一步？", "project": "/Users/USER/work/demo"},
        policy_path=str(alt_policy),
    )

    assert result["continuation_injected"] is True
