from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.integration import bridge as bridge_runtime
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
from keep_going.integration.bridge import enable_project, handle_stop_hook, render_stop_hook_output, run_self_test, status_project
from keep_going.decision.policy_runtime import compile_runtime_policy


CODEX_STOP_COMMON_OUTPUT_FIELDS = {"systemMessage", "continue", "stopReason"}


def _assert_valid_codex_stop_output(payload: dict[str, object]) -> None:
    allowed = CODEX_STOP_COMMON_OUTPUT_FIELDS | {"decision", "reason"}
    assert set(payload).issubset(allowed)
    assert "hookSpecificOutput" not in payload
    if payload.get("decision") == "block":
        assert isinstance(payload.get("reason"), str)
        assert str(payload["reason"]).strip()
    else:
        assert "decision" not in payload


def _append_codex_messages(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for role, text in rows:
            f.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": role,
                            "content": [{"type": "text", "text": text}],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _minimal_policy() -> dict[str, object]:
    return {
        "version": 0.4,
        "core_principles": [{"id": "verification", "statement": "先验证再交付。"}],
        "preferences": {},
        "redlines": [],
        "stop_decision": {
            "rules": [
                {
                    "id": "stop-no-message",
                    "action": "allow",
                    "reason": "stop_event_without_assistant_message",
                    "confidence": 0.9,
                    "derived_from": "scope-fidelity",
                    "when": {"message_empty": True},
                },
                {
                    "id": "when-stop-hook-sees-own-feedback",
                    "action": "allow",
                    "reason": "stop_self_feedback_allows_end",
                    "confidence": 0.95,
                    "derived_from": ["convergent-iteration", "ai-autonomy-as-north-star"],
                    "markers": [
                        "Keep Going 已按项目级 Stop hook 代用户给出轻量决策",
                        "请把上面内容当作用户回复继续处理",
                    ],
                },
                {
                    "id": "when-stop-hook-sees-completed-report",
                    "action": "allow",
                    "reason": "stop_completed_report_allows_end",
                    "confidence": 0.9,
                    "derived_from": ["outcome-only-care", "convergent-iteration"],
                    "completion_markers": ["本轮完成度", "主线目标", "关键改动", "已验证", "未完成或阻塞", "下一步建议", "下一刀建议"],
                    "min_completion_markers": 3,
                    "no_pending_markers": ["没有未完成", "等待真人", "主线已完成"],
                },
                {
                    "id": "stop-risk-needs-human",
                    "action": "escalate",
                    "reason": "stop_risk_needs_human",
                    "confidence": 0.92,
                    "derived_from": "outcome-only-care",
                    "principles_applied": ["outcome-only-care", "do-not-delete-unrecoverable", "do-not-leak-secrets"],
                    "heuristics_applied": ["git-write-needs-authorization"],
                    "terms": ["commit", "push", "提交", "推送", "删除", "token"],
                },
                {
                    "id": "stop-no-decision-needed",
                    "action": "allow",
                    "reason": "stop_no_user_decision_needed",
                    "confidence": 0.8,
                    "derived_from": ["scope-fidelity", "convergent-iteration"],
                },
                {
                    "id": "stop-lightweight-decision",
                    "action": "block",
                    "reason": "stop_lightweight_decision",
                    "confidence": 0.8,
                    "derived_from": ["ai-autonomy-as-north-star", "outcome-only-care"],
                    "reply": "继续。按最小必要改动推进，先确保有产物级验证；不要做无关重构或未授权的 git 操作。",
                    "terms": ["要不要", "是否", "选哪个", "哪种", "验证够", "能不能提交", "commit", "push", "proceed", "?", "？"],
                    "patterns": [r"(继续|下一步).{0,12}(吗|么|\?|？)", r"(要|是否|能否|可以).{0,12}继续"],
                },
            ]
        },
    }


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


def _write_fixture(cfg: Config) -> None:
    cfg.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(_minimal_policy(), allow_unicode=True),
        encoding="utf-8",
    )
    compile_runtime_policy(source)
    labels = cfg.paths.data_dir / "labels" / "labeled.jsonl"
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(
        json.dumps(
            {
                "turn_id": "turn1",
                "project": str(cfg.root),
                "role": "user",
                "content": "继续，先验证最终产物。",
                "prev_assistant": "要不要继续最终验证？",
                "labels": ["verification-demand"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_policy_only(cfg: Config) -> None:
    cfg.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(_minimal_policy(), allow_unicode=True),
        encoding="utf-8",
    )
    compile_runtime_policy(source)


def _decision_command(
    *,
    action: str,
    reply: str = "",
    reason: str,
    confidence: float = 0.9,
) -> str:
    payload = {
        "action": action,
        "reply": reply,
        "reason": reason,
        "confidence": confidence,
        "category": "preference",
        "evidence": [{"source": "test-cli", "id": reason}],
    }
    script = f"import json,sys; sys.stdin.read(); print({json.dumps(json.dumps(payload, ensure_ascii=False))})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _safe_stop_event(tmp_path: Path, message: str) -> dict[str, object]:
    transcript = tmp_path / "safe-stop.jsonl"
    _write_codex_transcript(
        transcript,
        [("user", "继续当前低风险任务，不执行 Git 写操作。"), ("assistant", message)],
    )
    return {
        "cwd": str(tmp_path),
        "session_id": "safe-stop",
        "transcript_path": str(transcript),
        "last_assistant_message": message,
    }


def test_bridge_enable_disable_status_are_project_scoped(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state"

    before = status_project(project, state_home=state_home)
    enabled = enable_project(
        project,
        host="claude-code",
        backend="cli",
        command="c 0",
        shell=True,
        state_home=state_home,
    )

    assert before["enabled"] is False
    assert enabled["enabled"] is True
    assert enabled["project"] == str(project)
    assert enabled["backend"] == "cli"
    assert enabled["command"] == "c 0"
    assert enabled["shell"] is True
    assert Path(enabled["state_file"]).exists()


def test_bridge_enable_defaults_to_codex_cli_backend(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state"

    enabled = enable_project(project, host="codex", state_home=state_home)

    assert enabled["enabled"] is True
    assert enabled["host"] == "codex"
    assert enabled["backend"] == "cli"
    assert "codex exec" in enabled["command"]
    assert enabled["input_mode"] == "stdin"


def test_existing_direct_stop_state_is_promoted_to_cli_backend(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", backend="direct", state_home=state_home)
    calls: list[dict[str, object]] = []

    def fake_decide_stop(
        cfg_arg: Config,
        event_arg: dict[str, object],
        *,
        project_path: Path,
        top_k: int,
        generate: bool,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "action": "allow",
            "reply": "",
            "reason": "cli_model_allowed",
            "confidence": 0.81,
            "evidence": [{"source": "test", "id": "promoted-cli"}],
        }

    monkeypatch.setattr(bridge_runtime, "decide_stop", fake_decide_stop)

    result = handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续最终验证？"},
        host="codex",
        state_home=state_home,
    )
    status = status_project(tmp_path, state_home=state_home)

    assert calls
    assert calls[0]["backend"] == "cli"
    assert "codex exec" in str(calls[0]["command"])
    assert result["backend"] == "cli"
    assert result["reason"] == "cli_model_allowed"
    assert status["backend"] == "cli"
    assert "codex exec" in status["command"]


def test_bridge_state_resolves_to_project_root(tmp_path: Path):
    project = tmp_path / "project"
    subdir = project / "src" / "pkg"
    subdir.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    state_home = tmp_path / "state"

    root_state = enable_project(project, state_home=state_home)
    subdir_state = status_project(subdir, state_home=state_home)

    assert root_state["project"] == str(project)
    assert subdir_state["project"] == str(project)
    assert subdir_state["enabled"] is True
    assert subdir_state["state_file"] == root_state["state_file"]


def test_stop_hook_noops_until_project_is_enabled(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    event = {"cwd": str(tmp_path), "last_assistant_message": "要不要继续最终验证？"}

    result = handle_stop_hook(cfg, event, host="claude-code", state_home=tmp_path / "state")

    assert result["action"] == "allow"
    assert result["reason"] == "project disabled"
    assert result["host_response"] is None
    assert render_stop_hook_output(result) == ""


def test_enabled_default_stop_hook_delegates_allow_decisions_to_cli_stop_decision(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", state_home=state_home)
    calls: list[dict[str, object]] = []

    def fake_decide_stop(
        cfg_arg: Config,
        event_arg: dict[str, object],
        *,
        project_path: Path,
        top_k: int,
        generate: bool,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(
            {
                "cfg": cfg_arg,
                "event": event_arg,
                "project_path": project_path,
                "top_k": top_k,
                "generate": generate,
                "kwargs": kwargs,
            }
        )
        return {
            "action": "allow",
            "reply": "",
            "reason": "decision_allowed_completed_report",
            "confidence": 0.77,
            "evidence": [{"source": "test", "id": "stop-decision"}],
        }

    monkeypatch.setattr(bridge_runtime, "decide_stop", fake_decide_stop)

    result = handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "当前没有未完成的实现主线需要继续推进。"},
        host="codex",
        state_home=state_home,
    )

    assert len(calls) == 1
    assert calls[0]["cfg"] is cfg
    assert calls[0]["project_path"] == tmp_path
    assert calls[0]["top_k"] == 5
    assert calls[0]["generate"] is False
    assert calls[0]["kwargs"]["backend"] == "cli"
    assert "codex exec" in str(calls[0]["kwargs"]["command"])
    assert calls[0]["kwargs"]["shell"] is False
    assert calls[0]["kwargs"]["input_mode"] == "stdin"
    assert calls[0]["kwargs"]["force_skill"] == "keep-going"
    assert calls[0]["kwargs"]["shell_executable"] == ""
    assert result["action"] == "allow"
    assert result["reason"] == "decision_allowed_completed_report"
    assert result["decision_result"] == {
        "action": "allow",
        "reply": "",
        "reason": "decision_allowed_completed_report",
        "confidence": 0.77,
        "evidence": [{"source": "test", "id": "stop-decision"}],
    }
    assert json.loads(render_stop_hook_output(result)) == {
        "systemMessage": "Keep Going Stop hook: allow (decision_allowed_completed_report, confidence=0.77)"
    }


def test_enabled_stop_hook_adds_rolling_decision_context_to_decision_event(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "sess-ctx", "cwd": str(tmp_path)}}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    _append_codex_messages(
        transcript,
        [
            ("user", "第一轮：只计划，不执行。"),
            ("assistant", "计划已完成。要不要开始实现？"),
        ],
    )
    enable_project(tmp_path, host="codex", state_home=state_home)
    calls: list[dict[str, object]] = []

    def fake_decide_stop(
        cfg_arg: Config,
        event_arg: dict[str, object],
        *,
        project_path: Path,
        top_k: int,
        generate: bool,
        **kwargs: object,
    ) -> dict[str, object]:
        del cfg_arg, project_path, top_k, generate, kwargs
        calls.append(event_arg)
        return {
            "action": "allow",
            "reply": "",
            "reason": "captured_context",
            "confidence": 0.9,
            "evidence": [],
        }

    monkeypatch.setattr(bridge_runtime, "decide_stop", fake_decide_stop)

    handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "transcript_path": str(transcript), "session_id": "sess-ctx", "last_assistant_message": "要不要开始实现？"},
        host="codex",
        state_home=state_home,
    )
    first_context = calls[-1]["decision_context"]
    first_offset = first_context["source"]["last_offset"]

    _append_codex_messages(
        transcript,
        [
            ("user", "第二轮：开始实现，但不要提交。"),
            ("assistant", "实现完成。要不要跑验证？"),
        ],
    )
    handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "transcript_path": str(transcript), "session_id": "sess-ctx", "last_assistant_message": "要不要跑验证？"},
        host="codex",
        state_home=state_home,
    )
    second_context = calls[-1]["decision_context"]

    assert first_context["context_status"] == "initialized"
    assert first_context["latest_user_goal"] == "第一轮：只计划，不执行。"
    assert second_context["context_status"] == "incremental"
    assert second_context["latest_user_goal"] == "第二轮：开始实现，但不要提交。"
    assert second_context["source"]["read_from_offset"] == first_offset
    assert Path(second_context["source"]["cache_path"]).exists()


def test_enabled_cli_stop_hook_delegates_to_same_stop_decision_interface(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        backend="cli",
        command="omxm",
        shell=True,
        input_mode="stdin",
        state_home=state_home,
    )
    calls: list[dict[str, object]] = []

    def fake_decide_stop(
        cfg_arg: Config,
        event_arg: dict[str, object],
        *,
        project_path: Path,
        top_k: int,
        generate: bool,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(
            {
                "cfg": cfg_arg,
                "event": event_arg,
                "project_path": project_path,
                "top_k": top_k,
                "generate": generate,
                "kwargs": kwargs,
            }
        )
        return {
            "action": "block",
            "reply": "继续跑验证。",
            "reason": "cli_stop_decision",
            "confidence": 0.88,
            "category": "preference",
            "evidence": [{"source": "test", "id": "cli-stop-decision"}],
        }

    monkeypatch.setattr(bridge_runtime, "decide_stop", fake_decide_stop)

    result = handle_stop_hook(
        cfg,
        _safe_stop_event(tmp_path, "要不要继续？"),
        host="codex",
        state_home=state_home,
    )

    assert len(calls) == 1
    assert calls[0]["cfg"] is cfg
    assert calls[0]["project_path"] == tmp_path
    assert calls[0]["top_k"] == 5
    assert calls[0]["generate"] is False
    assert calls[0]["kwargs"]["backend"] == "cli"
    assert calls[0]["kwargs"]["command"] == "omxm"
    assert calls[0]["kwargs"]["shell"] is True
    assert calls[0]["kwargs"]["input_mode"] == "stdin"
    assert calls[0]["kwargs"]["force_skill"] == "keep-going"
    assert calls[0]["kwargs"]["shell_executable"] == ""
    assert "policy_path" in calls[0]["kwargs"]
    assert result["action"] == "block"
    assert "继续跑验证" in result["host_response"]["reason"]


def test_direct_backend_result_uses_stop_decision_schema(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)

    result = decide_stop(
        cfg,
        _safe_stop_event(tmp_path, "要不要继续最终验证？"),
        project_path=tmp_path,
    )

    assert {"action", "reply", "reason", "confidence", "evidence"}.issubset(result)
    assert result["action"] in {"allow", "block", "escalate"}
    assert isinstance(result["reply"], str)
    assert isinstance(result["reason"], str)
    assert isinstance(result["confidence"], float)
    assert isinstance(result["evidence"], list)


def test_bridge_direct_path_no_longer_imports_legacy_stop_policy() -> None:
    source = Path(bridge_runtime.__file__).read_text(encoding="utf-8")

    assert "decide_stop_action" not in source


def test_enabled_cli_stop_hook_blocks_with_keep_going_reply(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="claude-code",
        command=_decision_command(action="block", reply="继续跑验证。", reason="cli_model_blocked"),
        state_home=state_home,
    )

    result = handle_stop_hook(
        cfg,
        _safe_stop_event(tmp_path, "要不要继续最终验证？"),
        host="claude-code",
        state_home=state_home,
    )

    assert result["action"] == "block"
    assert result["host_response"]["decision"] == "block"
    assert "Keep Going" in result["host_response"]["reason"]
    assert result["decision_result"]["reply"].startswith("继续")
    events = tmp_path / "events" / "stop-hook.jsonl"
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "block"
    assert rows[-1]["continuation_injected"] is True
    assert rows[-1]["escalate"] is False
    assert rows[-1]["question_sha1"]


def test_stop_hook_can_skip_metrics_for_synthetic_probe(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="claude-code",
        command=_decision_command(action="block", reply="继续跑验证。", reason="cli_model_blocked"),
        state_home=state_home,
    )

    result = handle_stop_hook(
        cfg,
        {**_safe_stop_event(tmp_path, "要不要继续最终验证？"), "hook_event_name": "Stop"},
        host="claude-code",
        state_home=state_home,
        record_metrics=False,
    )

    assert result["action"] == "block"
    assert result["metrics_recorded"] is False
    assert not (tmp_path / "events" / "stop-hook.jsonl").exists()


def test_enabled_stop_hook_allows_completed_status_report(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="allow", reason="stop_completed_report_allows_end", confidence=0.9),
        state_home=state_home,
    )
    report = "\n".join(
        [
            "当前没有未完成的实现主线需要继续推进。",
            "- 本轮完成度：100%",
            "- 主线目标：完成",
            "- 关键改动：无",
            "- 已验证：bridge self-test PASS",
            "- 未完成或阻塞：无",
            "- 下一步建议：等待真人新的任务目标。",
        ]
    )

    result = handle_stop_hook(cfg, {"cwd": str(tmp_path), "last_assistant_message": report}, host="codex", state_home=state_home)

    assert result["action"] == "allow"
    assert result["reason"] == "stop_completed_report_allows_end"
    assert result["decision_result"]["action"] == "allow"
    output = json.loads(render_stop_hook_output(result))
    _assert_valid_codex_stop_output(output)
    assert output == {"systemMessage": "Keep Going Stop hook: allow (stop_completed_report_allows_end, confidence=0.90)"}


def test_codex_stop_allow_output_matches_codex_stop_schema(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="allow", reason="stop_completed_report_allows_end", confidence=0.9),
        state_home=state_home,
    )
    report = "\n".join(
        [
            "当前没有未完成的实现主线需要继续推进。",
            "- 本轮完成度：100%",
            "- 主线目标：完成",
            "- 关键改动：无",
            "- 已验证：bridge self-test PASS",
            "- 未完成或阻塞：无",
            "- 下一步建议：等待真人新的任务目标。",
        ]
    )

    result = handle_stop_hook(cfg, {"cwd": str(tmp_path), "last_assistant_message": report}, host="codex", state_home=state_home)

    assert result["action"] == "allow"
    payload = json.loads(render_stop_hook_output(result))
    _assert_valid_codex_stop_output(payload)
    assert payload["systemMessage"] == "Keep Going Stop hook: allow (stop_completed_report_allows_end, confidence=0.90)"


def test_enabled_claude_stop_hook_allows_without_unsupported_additional_context(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="claude-code",
        command=_decision_command(action="allow", reason="stop_completed_report_allows_end", confidence=0.9),
        state_home=state_home,
    )
    report = "\n".join(
        [
            "当前没有未完成的实现主线需要继续推进。",
            "- 本轮完成度：100%",
            "- 主线目标：完成",
            "- 关键改动：无",
            "- 已验证：bridge self-test PASS",
            "- 未完成或阻塞：无",
            "- 下一步建议：等待真人新的任务目标。",
        ]
    )

    result = handle_stop_hook(cfg, {"cwd": str(tmp_path), "last_assistant_message": report}, host="claude-code", state_home=state_home)

    assert result["action"] == "allow"
    output = json.loads(render_stop_hook_output(result))
    assert output == {"systemMessage": "Keep Going Stop hook: allow (stop_completed_report_allows_end, confidence=0.90)"}


def test_enabled_stop_hook_allows_prior_keep_going_feedback(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="allow", reason="stop_self_feedback_allows_end", confidence=0.95),
        state_home=state_home,
    )
    feedback = (
        "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：继续。"
        "请把上面内容当作用户回复继续处理；如果后续触及提交、推送等高风险动作，仍需真人确认。"
    )

    result = handle_stop_hook(cfg, {"cwd": str(tmp_path), "last_assistant_message": feedback}, host="codex", state_home=state_home)

    assert result["action"] == "allow"
    assert result["reason"] == "stop_self_feedback_allows_end"
    assert result["decision_result"]["action"] == "allow"


def test_enabled_stop_hook_escalates_dangerous_action_without_injection(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="escalate", reason="stop_risk_needs_human", confidence=0.92),
        state_home=state_home,
    )

    result = handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "我已经改完了，是否现在 commit 并 push？"},
        host="codex",
        state_home=state_home,
    )

    assert result["action"] == "allow"
    assert result["reason"].startswith("safety_gate_risk_flags:")
    assert result["host_response"] is None
    assert result["decision_result"]["action"] == "escalate"


def test_safety_gate_rejects_risky_assistant_question_even_when_model_miscategorizes(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(
            action="block",
            reply="继续强推。",
            reason="model_miscategorized_as_preference",
            confidence=0.9,
        ),
        state_home=state_home,
    )

    stale_transcript = tmp_path / "stale-stop.jsonl"
    _write_codex_transcript(
        stale_transcript,
        [("user", "继续准备发布说明。"), ("assistant", "正在整理安全的发布说明。")],
    )
    result = handle_stop_hook(
        cfg,
        {
            "cwd": str(tmp_path),
            "session_id": "stale-stop",
            "transcript_path": str(stale_transcript),
            "last_assistant_message": "要不要 git push --force origin main？",
        },
        host="codex",
        state_home=state_home,
    )

    assert result["action"] == "allow"
    assert result["host_response"] is None
    assert result["decision_result"]["action"] == "escalate"
    assert "safety_gate_risk_flags" in result["decision_result"]["reason"]


def test_cli_backend_can_call_configured_local_command(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    script = (
        "import json,sys; sys.stdin.read(); "
        "print(json.dumps({'action': 'block', 'reply': '继续跑验证。', 'reason': 'cli_backend', 'confidence': 0.9, 'category': 'preference', 'evidence': []}, ensure_ascii=False))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    enable_project(tmp_path, host="codex", backend="cli", command=command, state_home=state_home)

    result = handle_stop_hook(
        cfg,
        _safe_stop_event(tmp_path, "要不要继续？"),
        host="codex",
        state_home=state_home,
    )

    assert result["action"] == "block"
    assert result["host"] == "codex"
    assert "继续跑验证" in result["host_response"]["reason"]


def test_cli_backend_prompt_contains_stop_decision_policy(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)
    script = (
        "import json,sys; prompt=sys.stdin.read(); "
        "print(json.dumps({'action':'allow','reply':'','reason':'saw_policy','confidence':0.9,"
        "'evidence':[{'source':'prompt','id':'has-policy','has_stop_decision':'stop_decision' in prompt,"
        "'has_rule':'stop-lightweight-decision' in prompt,"
        "'has_runtime_schema':'runtime_schema_version' in prompt,"
        "'has_provenance':'derived_from' in prompt}]}, ensure_ascii=False))"
    )

    result = decide_stop(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
        project_path=tmp_path,
        backend="cli",
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
    )

    evidence = result["evidence"][0]
    assert evidence["has_stop_decision"] is True
    assert evidence["has_rule"] is True
    assert evidence["has_runtime_schema"] is True
    assert evidence["has_provenance"] is False


def test_cli_backend_prompt_uses_bounded_decision_context_and_minimal_raw_event(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)
    script = (
        "import json,sys; prompt=sys.stdin.read(); "
        "print(json.dumps({'action':'allow','reply':'','reason':'prompt_checked','confidence':0.9,"
        "'evidence':[{'source':'prompt','id':'bounded-context',"
        "'has_decision_context':'\"decision_context\"' in prompt,"
        "'has_raw_event_minimal':'\"raw_event_minimal\"' in prompt,"
        "'has_raw_event_key':'\"raw_event\"' in prompt,"
        "'has_huge_value':'RAW_EVENT_SHOULD_NOT_LEAK' in prompt,"
        "'prompt_bytes':len(prompt.encode('utf-8'))}]}, ensure_ascii=False))"
    )

    result = decide_stop(
        cfg,
        {
            "cwd": str(tmp_path),
            "last_assistant_message": "要不要继续？",
            "transcript_path": str(tmp_path / "rollout.jsonl"),
            "session_id": "sess-prompt",
            "decision_context": {"latest_user_goal": "继续实现", "context_confidence": "high"},
            "huge": "RAW_EVENT_SHOULD_NOT_LEAK" * 5000,
        },
        project_path=tmp_path,
        backend="cli",
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
    )

    evidence = result["evidence"][0]
    assert evidence["has_decision_context"] is True
    assert evidence["has_raw_event_minimal"] is True
    assert evidence["has_raw_event_key"] is False
    assert evidence["has_huge_value"] is False
    assert evidence["prompt_bytes"] < 65_536


def test_cli_backend_suppresses_nested_keep_going_stop_hook(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)
    script = (
        "import json,os,sys; sys.stdin.read(); "
        "print(json.dumps({'action':'allow','reply':'','reason':'env_checked','confidence':0.9,"
        "'evidence':[{'source':'env','id':'suppression','value':os.environ.get('KEEP_GOING_STOP_HOOK_SUPPRESS','')}]}, ensure_ascii=False))"
    )

    result = decide_stop(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
        project_path=tmp_path,
        backend="cli",
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
    )

    assert result["evidence"][0]["value"] == "1"


def test_cli_backend_timeout_escalates_without_crashing(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    monkeypatch.setenv("KEEP_GOING_STOP_CLI_TIMEOUT_SECONDS", "0.01")
    script = "import sys,time; sys.stdin.read(); time.sleep(2)"
    enable_project(
        tmp_path,
        host="codex",
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        state_home=state_home,
    )

    result = handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续最终验证？"},
        host="codex",
        state_home=state_home,
    )

    assert result["action"] == "allow"
    assert result["reason"] == "cli_backend_failed"
    assert result["host_response"] is None
    assert result["decision_result"]["action"] == "escalate"
    assert result["decision_result"]["confidence"] == 0.0
    assert result["decision_result"]["evidence"][0]["id"] == "RuntimeError"
    assert "timed out" in result["decision_result"]["evidence"][0]["detail"]
    events = tmp_path / "events" / "stop-hook.jsonl"
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "allow"
    assert rows[-1]["escalate"] is True


def test_bridge_cli_stop_hook_emits_host_response(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        command=_decision_command(action="block", reply="继续跑验证。", reason="cli_model_blocked"),
        state_home=state_home,
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(
        cli.main,
        ["bridge", "stop-hook", "--input-json", "--state-home", str(state_home)],
        input=json.dumps(_safe_stop_event(tmp_path, "要不要继续最终验证？"), ensure_ascii=False),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["decision"] == "block"
    assert "Keep Going" in payload["reason"]


def test_bridge_cli_stop_hook_emits_allow_status_without_block(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="allow", reason="stop_completed_report_allows_end", confidence=0.9),
        state_home=state_home,
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    report = "\n".join(
        [
            "当前没有未完成的实现主线需要继续推进。",
            "- 本轮完成度：100%",
            "- 主线目标：完成",
            "- 关键改动：无",
            "- 已验证：bridge self-test PASS",
            "- 未完成或阻塞：无",
            "- 下一步建议：等待真人新的任务目标。",
        ]
    )

    result = CliRunner().invoke(
        cli.main,
        ["bridge", "stop-hook", "--input-json", "--state-home", str(state_home)],
        input=json.dumps({"cwd": str(tmp_path), "last_assistant_message": report}, ensure_ascii=False),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    _assert_valid_codex_stop_output(payload)
    assert payload == {"systemMessage": "Keep Going Stop hook: allow (stop_completed_report_allows_end, confidence=0.90)"}


def test_bridge_cli_stop_hook_emits_claude_allow_status_without_hook_specific_output(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="claude-code",
        command=_decision_command(action="allow", reason="stop_completed_report_allows_end", confidence=0.9),
        state_home=state_home,
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    report = "\n".join(
        [
            "当前没有未完成的实现主线需要继续推进。",
            "- 本轮完成度：100%",
            "- 主线目标：完成",
            "- 关键改动：无",
            "- 已验证：bridge self-test PASS",
            "- 未完成或阻塞：无",
            "- 下一步建议：等待真人新的任务目标。",
        ]
    )

    result = CliRunner().invoke(
        cli.main,
        ["bridge", "stop-hook", "--input-json", "--host", "claude-code", "--state-home", str(state_home)],
        input=json.dumps({"cwd": str(tmp_path), "last_assistant_message": report}, ensure_ascii=False),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"systemMessage": "Keep Going Stop hook: allow (stop_completed_report_allows_end, confidence=0.90)"}


def test_bridge_cli_stop_hook_synthetic_skips_metrics(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        command=_decision_command(action="block", reply="继续跑验证。", reason="cli_model_blocked"),
        state_home=state_home,
    )
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    result = CliRunner().invoke(
        cli.main,
        ["bridge", "stop-hook", "--input-json", "--state-home", str(state_home), "--synthetic", "--json-output"],
        input=json.dumps(
            {**_safe_stop_event(tmp_path, "要不要继续最终验证？"), "hook_event_name": "Stop"},
            ensure_ascii=False,
        ),
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["action"] == "block"
    assert payload["metrics_recorded"] is False
    assert not (tmp_path / "events" / "stop-hook.jsonl").exists()


def test_bridge_self_test_uses_temp_state(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    monkeypatch.setenv(
        "KEEP_GOING_CODEX_CLI_COMMAND",
        _decision_command(action="block", reply="继续跑验证。", reason="cli_model_blocked"),
    )

    report = run_self_test(cfg, project=tmp_path)

    assert report["passed"] is True
    assert report["host"] == "codex"
    assert report["disabled_action"] == "allow"
    assert report["enabled_action"] == "block"


def test_bridge_self_test_can_target_claude_code(monkeypatch, tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy_only(cfg)
    monkeypatch.setenv(
        "KEEP_GOING_CLAUDE_CODE_CLI_COMMAND",
        _decision_command(action="block", reply="继续跑验证。", reason="cli_model_blocked"),
    )

    report = run_self_test(cfg, project=tmp_path, host="claude-code")

    assert report["passed"] is True
    assert report["host"] == "claude-code"
    assert report["enabled_action"] == "block"


def test_stop_decision_uses_canonical_policy_by_default(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)

    result = decide_stop(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续最终验证？"},
        project_path=tmp_path,
    )

    assert result["action"] in {"allow", "block", "escalate"}
    assert isinstance(result["evidence"], list)


def test_stop_decision_uses_custom_policy_path(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)

    alt_policy = tmp_path / "alt-policy.yaml"
    alt_policy.parent.mkdir(parents=True, exist_ok=True)
    alt_policy.write_text(
        yaml.safe_dump(
            {
                "core_principles": [{"id": "alt", "statement": "替代。"}],
                "stop_decision": {
                    "rules": [
                        {
                            "id": "alt-allow-all",
                            "action": "allow",
                            "reason": "alt_policy_default_allow",
                            "confidence": 0.9,
                        }
                    ]
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    result = decide_stop(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续最终验证？"},
        project_path=tmp_path,
        policy_path=str(alt_policy),
    )

    assert result["action"] == "allow"
    assert result["reason"] == "alt_policy_default_allow"
    assert str(alt_policy) in str(result["evidence"])


def test_stop_decision_cli_backend_uses_custom_policy_path(tmp_path: Path):
    from keep_going.decision.stop_decision import decide_stop

    cfg = _config(tmp_path)
    _write_fixture(cfg)

    alt_policy = tmp_path / "alt-cli-policy.yaml"
    alt_policy.parent.mkdir(parents=True, exist_ok=True)
    alt_policy.write_text(
        yaml.safe_dump(
            {
                "core_principles": [{"id": "alt-cli", "statement": "CLI 替代。"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    script = (
        "import json,sys; prompt=sys.stdin.read(); "
        "print(json.dumps({'action':'allow','reply':'','reason':'alt_policy_path_used','confidence':0.9,"
        "'evidence':[{'source':'prompt','id':'policy-path-check','has_alt_path':"
        + json.dumps(str(alt_policy))
        + " in prompt}]}, ensure_ascii=False))"
    )

    result = decide_stop(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
        project_path=tmp_path,
        backend="cli",
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        policy_path=str(alt_policy),
    )

    assert result["reason"] == "alt_policy_path_used"
    assert result["evidence"][0]["has_alt_path"] is True


_KEEP_GOING_INJECTION_TEXT = (
    "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：选方案A。\n"
    "请把上面内容当作用户回复继续处理；如后续触及高风险动作仍需真人确认。"
)


def _write_codex_transcript(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"type": "session_meta", "payload": {"id": path.stem, "cwd": str(path.parent)}},
                ensure_ascii=False,
            )
            + "\n"
        )
        for role, text in rows:
            f.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": role, "content": [{"type": "text", "text": text}]},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def test_stop_hook_active_still_consults_keep_going_within_chain_depth(tmp_path: Path):
    # Regression: a continuation Stop (stop_hook_active=True) used to be hard
    # short-circuited to allow, so the Keep Going could never chain two replies. It
    # must now still consult the Keep Going (and may block) while depth < cap.
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="block", reply="选方案A，继续逐题。", reason="cli_model_blocked"),
        state_home=state_home,
    )
    transcript = tmp_path / "rollout.jsonl"
    _write_codex_transcript(
        transcript,
        [
            ("user", "真实目标：逐题确认设计，每题给推荐答案。"),
            ("assistant", "问题1：缓存层用方案A还是方案B？"),
            ("user", _KEEP_GOING_INJECTION_TEXT),
            ("assistant", "问题2：要不要给接口加限流？"),
        ],
    )
    event = {
        "hook_event_name": "Stop",
        "cwd": str(tmp_path),
        "session_id": "chain-sess",
        "transcript_path": str(transcript),
        "stop_hook_active": True,
        "last_assistant_message": "问题2：要不要给接口加限流？",
    }

    result = handle_stop_hook(cfg, event, host="codex", state_home=state_home)

    assert result["action"] == "block"
    assert result["decision_result"]["reply"].startswith("选方案A")
    assert result["continuation_chain_depth"] == 1
    rows = [json.loads(line) for line in (tmp_path / "events" / "stop-hook.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["stop_hook_active"] is True
    assert rows[-1]["continuation_chain_depth"] == 1
    assert rows[-1]["action"] == "block"


def test_stop_hook_active_hands_back_to_human_when_chain_depth_exceeded(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KEEP_GOING_STOP_MAX_CHAIN_DEPTH", "1")
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="block", reply="不应到达这里。", reason="cli_model_blocked"),
        state_home=state_home,
    )
    transcript = tmp_path / "rollout.jsonl"
    _write_codex_transcript(
        transcript,
        [
            ("user", "真实目标：逐题确认设计。"),
            ("assistant", "问题1：用方案A还是B？"),
            ("user", _KEEP_GOING_INJECTION_TEXT),
            ("assistant", "问题2：要不要加缓存？"),
        ],
    )
    event = {
        "hook_event_name": "Stop",
        "cwd": str(tmp_path),
        "session_id": "chain-cap",
        "transcript_path": str(transcript),
        "stop_hook_active": True,
        "last_assistant_message": "问题2：要不要加缓存？",
    }

    result = handle_stop_hook(cfg, event, host="codex", state_home=state_home)

    # depth (1) >= cap (1): the safety valve hands control back to the human,
    # overriding the block command the Keep Going backend would otherwise emit.
    assert result["action"] == "allow"
    assert result["reason"].startswith("stop_chain_depth_exceeded")
    assert result["decision_result"] is None


def test_stop_hook_active_hands_back_when_context_low_confidence(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_fixture(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command=_decision_command(action="block", reply="不应到达这里。", reason="cli_model_blocked"),
        state_home=state_home,
    )
    event = {
        "hook_event_name": "Stop",
        "cwd": str(tmp_path),
        "session_id": "no-transcript",
        "transcript_path": str(tmp_path / "missing.jsonl"),
        "stop_hook_active": True,
        "last_assistant_message": "要不要继续？",
    }

    result = handle_stop_hook(cfg, event, host="codex", state_home=state_home)

    # No readable transcript mid-chain: do not blind-block; hand back to human.
    assert result["action"] == "allow"
    assert result["reason"] == "stop_hook_active_low_confidence"
    assert result["decision_result"] is None
