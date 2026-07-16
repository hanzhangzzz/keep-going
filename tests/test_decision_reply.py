from __future__ import annotations

import json
import subprocess
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
from keep_going.eval.replay import _decision_alignment, run_eval
from keep_going.decision import reply as reply_mod
from keep_going.decision.policy_runtime import compile_runtime_policy
from keep_going.decision.reply import build_decision_reply, generate_reply_with_claude_cli, load_decision_policy, retrieve_examples


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


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
                "preferences": {"coding": [{"id": "minimum-diff", "pref": "改动越小越好。"}]},
                "heuristics": [
                    {"id": "when-failure-encountered", "trigger": "失败", "typical_response": "先要根因。"}
                ],
                "ai_collaboration_modes": [{"id": "executor", "when": "方案已对齐", "ai_should": ["彻底执行"]}],
                "redlines": [{"id": "do-not-fake-completion", "rule": "不要伪造完成。"}],
                "vocabulary": {"technical_idioms": ["闭环"], "tone": ["直接，少寒暄"]},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    compile_runtime_policy(path)


def _write_examples(path: Path) -> None:
    _write_jsonl(
        path,
        [
            {
                "turn_id": "evidence1",
                "ts": "2026-05-01T00:00:00Z",
                "project": "/Users/USER/work/demo",
                "role": "user",
                "content": "你确定吗？给我证据链和验证结果。",
                "prev_assistant": "已经修好了，应该可以了。",
                "labels": ["evidence-probe", "verification-demand"],
            },
            {
                "turn_id": "choice1",
                "ts": "2026-05-02T00:00:00Z",
                "project": "/Users/USER/work/other",
                "role": "user",
                "content": "选第二种吧。",
                "prev_assistant": "我有三种方案，第一种大改，第二种最小改动。",
                "labels": ["choice-among-options"],
            },
        ],
    )


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


def test_load_decision_policy(tmp_path: Path):
    policy_path = tmp_path / "artifacts" / "decision-policy.yaml"
    _write_policy(policy_path)

    policy = load_decision_policy(policy_path)

    assert policy["version"] == 0.4
    assert policy["core_principles"][0]["id"] == "evidence-first"


def test_retrieve_examples_scores_prev_assistant_and_project(tmp_path: Path):
    examples_path = tmp_path / "data" / "labels" / "labeled.jsonl"
    _write_examples(examples_path)

    examples = retrieve_examples(examples_path, "修好了？你确定有什么证据", "/Users/USER/work/demo", top_k=2)

    assert examples[0].turn_id == "evidence1"
    assert examples[0].score > examples[1].score


def test_build_decision_reply_returns_reply_package(tmp_path: Path):
    policy_path = tmp_path / "artifacts" / "decision-policy.yaml"
    examples_path = tmp_path / "data" / "labels" / "labeled.jsonl"
    _write_policy(policy_path)
    _write_examples(examples_path)

    result = build_decision_reply(
        question="已经修好了，是否继续下一步？",
        project="/Users/USER/work/demo",
        policy_path=policy_path,
        examples_path=examples_path,
        top_k=2,
        model="keep-going-model",
    )

    assert result["reply"].startswith("继续")
    assert result["confidence"] >= 0.5
    assert result["escalate"] is False
    assert "evidence-before-completion" in result["principles_applied"]
    assert result["few_shots"][0]["turn_id"] == "evidence1"
    assert "AI 的问题" in result["prompt"]
    assert "minimum-diff" in result["prompt"]
    assert "when-failure-encountered" in result["prompt"]
    assert "do-not-fake-completion" in result["prompt"]
    assert "闭环" in result["prompt"]
    assert result["generated"] is False


def test_build_decision_reply_can_use_generator(tmp_path: Path):
    policy_path = tmp_path / "artifacts" / "decision-policy.yaml"
    examples_path = tmp_path / "data" / "labels" / "labeled.jsonl"
    _write_policy(policy_path)
    _write_examples(examples_path)

    result = build_decision_reply(
        question="我有三个方案，你选哪个？",
        project="/Users/USER/work/demo",
        policy_path=policy_path,
        examples_path=examples_path,
        generate=True,
        generator=lambda prompt, model: f"{model}: 选最小可验证方案。",
        model="fake-model",
    )

    assert result["reply"] == "fake-model: 选最小可验证方案。"
    assert result["generated"] is True


def test_generate_reply_with_claude_cli_invokes_safe_cli_mode(monkeypatch):
    captured = {}

    monkeypatch.setattr(reply_mod.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setenv("KEEP_GOING_CLAUDE_CLI_MODEL", "haiku")
    monkeypatch.setenv("KEEP_GOING_CLAUDE_CLI_MAX_BUDGET_USD", "0.08")

    def fake_run(cmd, *, input, text, capture_output, check):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["text"] = text
        captured["capture_output"] = capture_output
        captured["check"] = check
        return subprocess.CompletedProcess(cmd, 0, stdout=" 继续。\n", stderr="")

    monkeypatch.setattr(reply_mod.subprocess, "run", fake_run)

    reply = generate_reply_with_claude_cli("prompt text", "sonnet-model")

    assert reply == "继续。"
    assert captured["input"] == "prompt text"
    assert captured["text"] is True
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert captured["cmd"] == [
        "/bin/claude",
        "-p",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--tools",
        "",
        "--setting-sources",
        "user",
        "--model",
        "haiku",
        "--max-budget-usd",
        "0.08",
    ]


def test_build_decision_reply_escalates_high_risk_operation(tmp_path: Path):
    policy_path = tmp_path / "artifacts" / "decision-policy.yaml"
    examples_path = tmp_path / "data" / "labels" / "labeled.jsonl"
    _write_policy(policy_path)
    _write_examples(examples_path)

    result = build_decision_reply(
        question="我要不要直接删除生产数据库？",
        project="/Users/USER/work/demo",
        policy_path=policy_path,
        examples_path=examples_path,
    )

    assert result["escalate"] is True
    assert result["reply"].startswith("这个需要真人确认")


def test_reply_cli_outputs_json(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(
        cli.main,
        ["reply", "--question", "要不要继续下一步？", "--project", "/Users/USER/work/demo"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["reply"].startswith("继续")
    assert payload["model"] == "keep-going-model"


def test_run_eval_outputs_markdown_report(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")

    out = run_eval(cfg, holdout_ratio=0.5, limit=1)

    text = out.read_text(encoding="utf-8")
    assert out.name.startswith("eval-")
    assert "eval_scope: lightweight_decision" in text
    assert "avg_decision_alignment" in text
    assert "choice1" in text
    assert "generated_mode: false" in text


def test_run_eval_filters_non_decision_turns(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_jsonl(
        cfg.paths.data_dir / "labels" / "labeled.jsonl",
        [
            {
                "turn_id": "decision1",
                "ts": "2026-05-01T00:00:00Z",
                "project": "/Users/USER/work/demo",
                "role": "user",
                "content": "继续，先验证再交付。",
                "prev_assistant": "要不要继续下一步？",
                "labels": ["execute-short"],
            },
            {
                "turn_id": "neutral1",
                "ts": "2026-05-02T00:00:00Z",
                "project": "/Users/USER/work/demo",
                "role": "user",
                "content": "这段文字再润色一下。",
                "prev_assistant": "我已经整理好总结文档。",
                "labels": ["writing-style"],
            },
            {
                "turn_id": "hook_noise",
                "ts": "2026-05-03T00:00:00Z",
                "project": "/Users/USER/work/demo",
                "role": "user",
                "content": "Stop hook feedback: internal transport failed",
                "prev_assistant": "要不要继续？",
                "labels": ["execute-short"],
            },
        ],
    )

    out = run_eval(cfg, holdout_ratio=1.0, limit=None)

    text = out.read_text(encoding="utf-8")
    assert "source_user_turns: 3" in text
    assert "eligible_user_turns: 1" in text
    assert "decision1" in text
    assert "neutral1" not in text
    assert "hook_noise" not in text


def test_run_eval_excludes_long_contentful_decision_turns(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_jsonl(
        cfg.paths.data_dir / "labels" / "labeled.jsonl",
        [
            {
                "turn_id": "short_decision",
                "ts": "2026-05-01T00:00:00Z",
                "project": "/Users/USER/work/demo",
                "role": "user",
                "content": "继续，先验证再交付。",
                "prev_assistant": "要不要继续下一步？",
                "labels": ["execute-short"],
            },
            {
                "turn_id": "long_spec",
                "ts": "2026-05-02T00:00:00Z",
                "project": "/Users/USER/work/demo",
                "role": "user",
                "content": "把字段 A 改成 B，字段 C 改成 D，补充说明和长段落。" * 8,
                "prev_assistant": "要不要按这个方案改？",
                "labels": ["scope-correction"],
            },
        ],
    )

    out = run_eval(cfg, holdout_ratio=1.0, limit=None)

    text = out.read_text(encoding="utf-8")
    assert "eligible_user_turns: 1" in text
    assert "short_decision" in text
    assert "long_spec" not in text


def test_decision_alignment_does_not_treat_double_unknown_as_match():
    assert _decision_alignment("纯内容 A", "另一段内容 B", False, 0.123) == 0.123


def test_run_eval_can_use_generator(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")

    out = run_eval(
        cfg,
        holdout_ratio=0.5,
        limit=1,
        generate=True,
        generator=lambda prompt, model: "选第二种吧。",
    )

    text = out.read_text(encoding="utf-8")
    assert "generated_mode: true" in text
    assert "选第二种吧。" in text


def test_eval_cli_outputs_report_path(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(cli.main, ["eval", "--holdout-ratio", "0.5", "--limit", "1"])

    assert result.exit_code == 0
    assert "wrote eval report" in result.output
    assert list((cfg.paths.data_dir / "eval").glob("eval-*.md"))


def test_eval_cli_can_select_claude_cli_generate_backend(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    captured = {}

    def fake_run_eval(*args, **kwargs):
        captured.update(kwargs)
        return tmp_path / "eval.md"

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "_run_eval", fake_run_eval)

    result = CliRunner().invoke(
        cli.main,
        ["eval", "--generate", "--generate-backend", "claude-cli", "--holdout-ratio", "0.5", "--limit", "1"],
    )

    assert result.exit_code == 0
    assert captured["generate"] is True
    assert captured["generator"] is cli.generate_reply_with_claude_cli


def test_reply_cli_accepts_input_json(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg.paths.artifacts_dir / "decision-policy.yaml")
    _write_examples(cfg.paths.data_dir / "labels" / "labeled.jsonl")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(
        cli.main,
        ["reply", "--input-json"],
        input=json.dumps({"question": "要不要继续？", "project": "/Users/USER/work/demo"}),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["reply"].startswith("继续")


def test_eval_holdout_dedupes_repeated_scheduled_prompts():
    from keep_going.eval.replay import _select_holdout

    rows = [
        {"content": "review open MR(sync-p0的MR除外)", "prev_assistant": "job 失败，重试第 %d 轮" % i}
        for i in range(8)
    ]
    rows += [
        {"content": "review open MR(sync-p0的MR除外)", "prev_assistant": "job 失败，重试第 0 轮"},
        {"content": "继续", "prev_assistant": "要不要继续？"},
    ]
    selected = _select_holdout(rows, 1.0, None)
    contents = [r["content"] for r in selected]
    assert contents == ["review open MR(sync-p0的MR除外)", "继续"]
