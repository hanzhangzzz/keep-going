from datetime import datetime, timezone

from keep_going.corpus.schema import Turn, make_turn_id


def test_turn_id_is_deterministic():
    a = make_turn_id("claude-code", "sess-1", 3)
    b = make_turn_id("claude-code", "sess-1", 3)
    assert a == b
    assert len(a) == 16


def test_turn_id_changes_with_input():
    assert make_turn_id("claude-code", "sess-1", 3) != make_turn_id("codex", "sess-1", 3)
    assert make_turn_id("claude-code", "sess-1", 3) != make_turn_id("claude-code", "sess-2", 3)


def test_turn_round_trip():
    t = Turn(
        turn_id="abc",
        source="claude-code",
        session_id="s",
        ts=datetime(2026, 5, 1, tzinfo=timezone.utc),
        project="/p",
        role="user",
        content="hi",
        prev_assistant=None,
        turn_idx=0,
    )
    j = t.model_dump_json()
    t2 = Turn.model_validate_json(j)
    assert t2.turn_id == t.turn_id
    assert t2.ts == t.ts
