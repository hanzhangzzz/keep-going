from __future__ import annotations

import math

import pytest

from keep_going.decision.stop_decision import normalize_stop_decision
from keep_going.decision.stop_safety import enforce_stop_safety_policy


def _decision(**overrides):
    return {
        "action": "block",
        "reply": "继续执行窄范围验证。",
        "reason": "model_decision",
        "confidence": 0.9,
        "evidence": [],
        "category": "preference",
        **overrides,
    }


@pytest.mark.parametrize("category", ["authorization", "information"])
def test_human_only_categories_cannot_block(category: str):
    result = enforce_stop_safety_policy(_decision(category=category), {})
    assert result["action"] == "escalate"
    assert result["reply"] == ""
    assert result["reason"] == f"safety_gate_human_only_category:{category}"


def test_risk_flag_overrides_model_miscategorization():
    result = enforce_stop_safety_policy(
        _decision(category="preference"),
        {"risk_flags": ["mentions:push"]},
    )
    assert result["action"] == "escalate"
    assert result["reason"] == "safety_gate_risk_flags:mentions:push"


def test_assistant_question_risk_overrides_model_miscategorization():
    result = enforce_stop_safety_policy(
        _decision(category="preference", confidence=0.9, reply="继续强推。"),
        {"pending_question": "要不要 git push --force origin main？"},
    )
    assert result["action"] == "escalate"
    assert "mentions:push" in result["reason"]


def test_current_event_risk_overrides_stale_context():
    result = enforce_stop_safety_policy(
        _decision(category="preference", confidence=0.9, reply="继续强推。"),
        {"current_progress": "正在准备发布说明。", "context_confidence": "high"},
        current_event={"last_assistant_message": "要不要 git push --force origin main？"},
    )
    assert result["action"] == "escalate"
    assert "mentions:push" in result["reason"]


@pytest.mark.parametrize(
    "event",
    [
        {"assistant_message": "要不要 git push --force origin main？"},
        {"assistant_response": "要不要 git push --force origin main？"},
        {"rawOutput": "要不要 git push --force origin main？"},
        {
            "last_assistant_message": (
                "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：\n"
                "现在 git push --force origin main？"
            )
        },
        {
            "last_assistant_message": (
                "引用：请把上面内容当作用户回复继续处理。\n"
                "现在执行 reset --hard 吗？"
            )
        },
    ],
)
def test_all_current_event_aliases_and_marker_quotes_are_scanned(event):
    result = enforce_stop_safety_policy(
        _decision(category="preference", confidence=0.9, reply="继续。"),
        {"context_confidence": "high"},
        current_event=event,
    )
    assert result["action"] == "escalate"
    assert result["reason"].startswith("safety_gate_risk_flags:")


def test_low_risk_preference_can_continue():
    decision = _decision()
    assert enforce_stop_safety_policy(decision, {"context_confidence": "high"}) == decision


def test_verification_continuation_can_continue_with_evidence_request():
    decision = _decision(category="verification", reply="先运行全量测试并贴出结果。")
    assert enforce_stop_safety_policy(decision, {"verification_state": "not_run"}) == decision


def test_low_confidence_block_escalates():
    result = enforce_stop_safety_policy(_decision(confidence=0.59), {})
    assert result["action"] == "escalate"
    assert result["reason"] == "safety_gate_low_confidence"


@pytest.mark.parametrize("confidence", [True, "0.9", math.nan, math.inf, -math.inf])
def test_non_numeric_or_non_finite_confidence_escalates(confidence):
    result = enforce_stop_safety_policy(_decision(confidence=confidence), {})
    assert result["action"] == "escalate"
    assert result["reason"] == "safety_gate_low_confidence"


@pytest.mark.parametrize("confidence", [True, "0.9", math.nan, math.inf, -math.inf])
def test_stop_decision_normalizer_rejects_invalid_confidence(confidence):
    result = normalize_stop_decision(
        _decision(confidence=confidence), fallback_reason="fallback"
    )
    assert result["confidence"] == 0.0


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (_decision(action="invalid"), "safety_gate_invalid_action"),
        (_decision(category=""), "safety_gate_missing_category"),
        (_decision(reply=""), "safety_gate_empty_block_reply"),
    ],
)
def test_malformed_or_empty_block_never_continues(decision: dict, reason: str):
    result = enforce_stop_safety_policy(decision, {})
    assert result["action"] == "escalate"
    assert result["reason"] == reason


def test_missing_transcript_never_continues():
    result = enforce_stop_safety_policy(
        _decision(),
        {"context_status": "missing_transcript", "context_confidence": "low"},
    )
    assert result["action"] == "escalate"
    assert result["reason"] == "safety_gate_context_unavailable"


def test_chain_depth_cap_never_continues():
    result = enforce_stop_safety_policy(
        _decision(),
        {"continuation_chain_depth": 8},
        max_chain_depth=8,
    )
    assert result["action"] == "escalate"
    assert result["reason"] == "safety_gate_chain_depth:8/8"
