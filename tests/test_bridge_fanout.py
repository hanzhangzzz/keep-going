"""U3 tests: state.json v2 migration, fan-out dispatch, merge logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

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
from keep_going.integration import bridge as bridge_runtime
from keep_going.integration.bridge import (
    enable_project,
    handle_stop_hook,
    merge_stop_decisions,
    status_project,
    _ensure_v2,
    _effective_action,
    _merge_evidence,
    _resolve_agent_policy,
)
from keep_going.decision.policy_runtime import compile_runtime_policy


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _minimal_policy() -> dict:
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
                    "id": "stop-lightweight-decision",
                    "action": "block",
                    "reason": "stop_lightweight_decision",
                    "confidence": 0.8,
                    "derived_from": "ai-autonomy-as-north-star",
                    "reply": "继续。",
                    "terms": ["要不要", "?"],
                },
            ]
        },
    }


def _write_policy(cfg: Config) -> None:
    cfg.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    source = cfg.paths.artifacts_dir / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(_minimal_policy(), allow_unicode=True), encoding="utf-8"
    )
    compile_runtime_policy(source)


def _block_result(*, reply: str = "继续。", confidence: float = 0.8, reason: str = "test_block") -> dict:
    return {
        "action": "block",
        "reply": reply,
        "reason": reason,
        "confidence": confidence,
        "category": "preference",
        "evidence": [{"source": "test", "id": reason}],
    }


def _allow_result(*, confidence: float = 0.7, reason: str = "test_allow") -> dict:
    return {
        "action": "allow",
        "reply": "",
        "reason": reason,
        "confidence": confidence,
        "category": "preference",
        "evidence": [{"source": "test", "id": reason}],
    }


def _escalate_result(*, confidence: float = 0.9, reason: str = "test_escalate") -> dict:
    return {
        "action": "escalate",
        "reply": "",
        "reason": reason,
        "confidence": confidence,
        "category": "authorization",
        "evidence": [{"source": "test", "id": reason}],
    }


def _safe_stop_event(tmp_path: Path, message: str) -> dict:
    transcript = tmp_path / "safe-stop.jsonl"
    rows = [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "text", "text": "继续当前低风险任务。"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": message}]}},
    ]
    transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return {
        "cwd": str(tmp_path),
        "session_id": "safe-stop",
        "transcript_path": str(transcript),
        "last_assistant_message": message,
    }


# ── _ensure_v2 migration ─────────────────────────────────────────────────────

def test_ensure_v2_adds_agents_and_render_mode_to_v1_state():
    v1_state = {"version": 1, "enabled": False}
    migrated = _ensure_v2(v1_state)
    assert migrated["version"] == 2
    assert migrated["agents"] == ["default"]
    assert migrated["render_mode"] == "block"


def test_ensure_v2_preserves_existing_v2_fields():
    v2_state = {"version": 2, "agents": ["qa-keep_going"], "render_mode": "advisory", "enabled": True}
    migrated = _ensure_v2(v2_state)
    assert migrated["agents"] == ["qa-keep_going"]
    assert migrated["render_mode"] == "advisory"  # preserves explicit advisory


def test_ensure_v2_fills_missing_fields_in_partial_v2():
    v2_state = {"version": 2, "enabled": True}
    migrated = _ensure_v2(v2_state)
    assert migrated["agents"] == ["default"]
    assert migrated["render_mode"] == "block"


# ── enable_project with agents/render_mode ────────────────────────────────────

def test_enable_project_stores_agents_in_state(tmp_path: Path):
    state_home = tmp_path / "state"
    result = enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        agents=["qa-keep_going", "arch-keep_going"],
    )
    assert result["agents"] == ["qa-keep_going", "arch-keep_going"]


def test_enable_project_stores_render_mode_in_state(tmp_path: Path):
    state_home = tmp_path / "state"
    result = enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        render_mode="advisory",
    )
    assert result["render_mode"] == "advisory"


def test_enable_project_defaults_to_default_agent_and_block_render(tmp_path: Path):
    state_home = tmp_path / "state"
    result = enable_project(tmp_path, host="codex", command="echo test", state_home=state_home)
    assert result["agents"] == ["default"]
    assert result["render_mode"] == "block"


def test_enable_project_rejects_agent_change_without_force(tmp_path: Path):
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", command="echo test", state_home=state_home, agents=["a"])
    try:
        enable_project(tmp_path, host="codex", command="echo test", state_home=state_home, agents=["b"])
    except ValueError as exc:
        assert "agent list changed" in str(exc)
    else:
        raise AssertionError("expected ValueError on agent change without --force")


def test_enable_project_allows_agent_change_with_force(tmp_path: Path):
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", command="echo test", state_home=state_home, agents=["a"])
    result = enable_project(
        tmp_path, host="codex", command="echo test", state_home=state_home, agents=["b"], force=True
    )
    assert result["agents"] == ["b"]


def test_status_project_migrates_v1_state_to_v2(tmp_path: Path):
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", command="echo test", state_home=state_home)
    status = status_project(tmp_path, state_home=state_home)
    state_file = Path(status["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state.pop("agents", None)
    state.pop("render_mode", None)
    state["version"] = 1
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    status = status_project(tmp_path, state_home=state_home)
    assert status["agents"] == ["default"]
    assert status["render_mode"] == "block"


# ── merge_stop_decisions ─────────────────────────────────────────────────────

def test_merge_empty_returns_allow():
    result = merge_stop_decisions([])
    assert result["action"] == "allow"
    assert result["reason"] == "agents_empty"


def test_merge_single_block():
    r = _block_result()
    merged = merge_stop_decisions([r])
    assert merged["action"] == "block"
    assert merged["reply"] == r["reply"]


def test_merge_escalate_beats_block():
    merged = merge_stop_decisions([_block_result(), _escalate_result()])
    assert merged["action"] == "escalate"


def test_merge_block_beats_allow():
    merged = merge_stop_decisions([_allow_result(), _block_result()])
    assert merged["action"] == "block"


def test_merge_all_allow_picks_highest_confidence():
    merged = merge_stop_decisions([_allow_result(confidence=0.5), _allow_result(confidence=0.9)])
    assert merged["action"] == "allow"
    assert merged["confidence"] == 0.9


def test_merge_picks_highest_confidence_within_tier():
    merged = merge_stop_decisions(
        [_block_result(confidence=0.6, reply="低"), _block_result(confidence=0.95, reply="高")]
    )
    assert merged["action"] == "block"
    assert merged["reply"] == "高"
    assert merged["confidence"] == 0.95


def test_merge_concats_evidence_with_agent_names():
    r1 = _allow_result(reason="a")
    r2 = _block_result(reason="b")
    merged = merge_stop_decisions([r1, r2], agent_names=["agent-a", "agent-b"])
    ev = merged["evidence"]
    assert any(e.get("agent") == "agent-a" for e in ev)
    assert any(e.get("agent") == "agent-b" for e in ev)


# ── _effective_action ────────────────────────────────────────────────────────

def test_effective_action_block_mode_passes_through():
    assert _effective_action("block", render_mode="block") == "block"
    assert _effective_action("allow", render_mode="block") == "allow"


def test_effective_action_advisory_mode_forces_allow():
    assert _effective_action("block", render_mode="advisory") == "allow"
    assert _effective_action("allow", render_mode="advisory") == "allow"


# ── _resolve_agent_policy ───────────────────────────────────────────────────────

def test_resolve_default_agent_returns_runtime_policy(tmp_path: Path):
    cfg = _config(tmp_path)
    policy_path = _resolve_agent_policy("default", tmp_path, cfg)
    assert policy_path == cfg.paths.artifacts_dir / "decision-policy.runtime.yaml"


def test_resolve_unknown_agent_falls_back_to_default_runtime(tmp_path: Path):
    cfg = _config(tmp_path)
    policy_path = _resolve_agent_policy("nonexistent-agent", tmp_path, cfg)
    assert policy_path == cfg.paths.artifacts_dir / "decision-policy.runtime.yaml"


def test_resolve_existing_project_agent_returns_its_policy(tmp_path: Path):
    cfg = _config(tmp_path)
    agent_dir = tmp_path / ".keep-going" / "agents" / "my-dna"
    agent_dir.mkdir(parents=True)
    policy_path = agent_dir / "policy-20260716T000000000Z.yaml"
    policy_path.write_text("version: 0.5\n", encoding="utf-8")
    (agent_dir / "meta.json").write_text(
        json.dumps({"name": "my-dna", "current_policy": str(policy_path)}),
        encoding="utf-8",
    )

    assert _resolve_agent_policy("my-dna", tmp_path, cfg) == policy_path


# ── loop guard ───────────────────────────────────────────────────────────────

def test_stop_hook_active_without_context_hands_back_to_human(tmp_path: Path):
    # A continuation Stop (stop_hook_active=True) no longer blindly short-circuits.
    # With no readable transcript the context is low-confidence, so the bounded
    # safety valve hands control back to the human instead of re-invoking the Keep Going.
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", command="echo test", state_home=state_home)

    result = handle_stop_hook(
        cfg,
        {"stop_hook_active": True, "cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
        host="codex",
        state_home=state_home,
    )
    assert result["action"] == "allow"
    assert result["reason"] == "stop_hook_active_low_confidence"


# ── single-agent fan-out path (N=1, short circuit) ───────────────────────────

def test_single_agent_uses_default_policy_path(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", command="echo test", state_home=state_home)

    calls: list[dict] = []

    def fake_decide(cfg_arg, event_arg, *, policy_path=None, **kwargs):
        calls.append({"policy_path": policy_path, **kwargs})
        return _block_result()

    with patch.object(bridge_runtime, "decide_stop", fake_decide):
        result = handle_stop_hook(
            cfg,
            _safe_stop_event(tmp_path, "要不要继续？"),
            host="codex",
            state_home=state_home,
        )

    assert result["action"] == "block"
    assert len(calls) == 1
    expected_policy = cfg.paths.artifacts_dir / "decision-policy.runtime.yaml"
    assert calls[0]["policy_path"] == expected_policy


# ── multi-agent fan-out (N≥2) ────────────────────────────────────────────────

def test_fanout_merges_two_agents(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        agents=["qa-keep_going", "arch-keep_going"],
    )

    call_count = 0

    def fake_decide(cfg_arg, event_arg, *, policy_path=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _allow_result(confidence=0.7, reason="qa_allows")
        return _block_result(confidence=0.85, reason="arch_blocks")

    with patch.object(bridge_runtime, "decide_stop", fake_decide):
        result = handle_stop_hook(
            cfg,
            _safe_stop_event(tmp_path, "要不要继续？"),
            host="codex",
            state_home=state_home,
        )

    assert result["action"] == "block"
    assert call_count == 2


def test_fanout_escalate_takes_priority(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        agents=["qa-keep_going", "arch-keep_going"],
    )

    call_count = 0

    def fake_decide(cfg_arg, event_arg, *, policy_path=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _block_result(confidence=0.9, reason="qa_blocks")
        return _escalate_result(confidence=0.85, reason="arch_escalates")

    with patch.object(bridge_runtime, "decide_stop", fake_decide):
        result = handle_stop_hook(
            cfg,
            {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
            host="codex",
            state_home=state_home,
        )

    assert result["action"] == "allow"
    assert result["decision_result"]["action"] == "escalate"


def test_fanout_advisory_mode_never_blocks(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        agents=["qa-keep_going", "arch-keep_going"],
        render_mode="advisory",
    )

    def fake_decide(cfg_arg, event_arg, *, policy_path=None, **kwargs):
        return _block_result(confidence=0.95)

    with patch.object(bridge_runtime, "decide_stop", fake_decide):
        result = handle_stop_hook(
            cfg,
            {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
            host="codex",
            state_home=state_home,
        )

    assert result["action"] == "allow"


def test_fanout_agent_name_invalid_short_circuits(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        agents=["qa-keep_going", "UPPERCASE"],
    )
    state_file = Path(status_project(tmp_path, state_home=state_home)["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["agents"] = ["qa-keep_going", "UPPERCASE"]
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    result = handle_stop_hook(
        cfg,
        {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
        host="codex",
        state_home=state_home,
    )

    assert result["action"] == "allow"
    assert "UPPERCASE" in result["reason"]


def test_fanout_sectioned_message_for_multi_agent_allow(tmp_path: Path):
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(
        tmp_path,
        host="codex",
        command="echo test",
        state_home=state_home,
        agents=["qa-keep_going", "arch-keep_going"],
        render_mode="advisory",
    )

    def fake_decide(cfg_arg, event_arg, *, policy_path=None, **kwargs):
        return _allow_result(confidence=0.7)

    with patch.object(bridge_runtime, "decide_stop", fake_decide):
        result = handle_stop_hook(
            cfg,
            {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
            host="codex",
            state_home=state_home,
        )

    assert result["action"] == "allow"
    assert result["decision_result"]["action"] == "allow"


def test_fanout_evidence_has_agent_labels(tmp_path: Path):
    r1 = _allow_result(reason="a")
    r2 = _block_result(reason="b")
    merged = merge_stop_decisions([r1, r2], agent_names=["qa", "arch"])
    for ev in merged["evidence"]:
        assert "agent" in ev
    agents_in_evidence = {ev["agent"] for ev in merged["evidence"]}
    assert agents_in_evidence == {"qa", "arch"}


def test_empty_agents_list_returns_allow_via_default_fallback(tmp_path: Path):
    """When state has agents=[] it falls back to ["default"] via _ensure_v2,
    so the single-agent path runs. Test that the fallback works correctly."""
    cfg = _config(tmp_path)
    _write_policy(cfg)
    state_home = tmp_path / "state"
    enable_project(tmp_path, host="codex", command="echo test", state_home=state_home)
    state_file = Path(status_project(tmp_path, state_home=state_home)["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["agents"] = []
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def fake_decide(cfg_arg, event_arg, *, policy_path=None, **kwargs):
        return _allow_result(confidence=0.8, reason="default_agent_allows")

    with patch.object(bridge_runtime, "decide_stop", fake_decide):
        result = handle_stop_hook(
            cfg,
            {"cwd": str(tmp_path), "last_assistant_message": "要不要继续？"},
            host="codex",
            state_home=state_home,
        )
    assert result["action"] == "allow"
