from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from click.testing import CliRunner

from keep_going import cli
from keep_going.decision.policy_runtime import compile_runtime_policy, load_runtime_policy
from keep_going.decision.reply import build_decision_reply
from keep_going.decision.stop_decision import build_stop_decision_prompt


def test_compile_runtime_policy_persists_reviewable_projection(tmp_path: Path) -> None:
    source = tmp_path / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "version": 0.5,
                "generated_at": "2026-07-15",
                "core_principles": [
                    {
                        "id": "evidence-first",
                        "statement": "先看证据。",
                        "derived_from": "outcome-only-care",
                        "evidence_turn_ids": ["turn-1"],
                    }
                ],
                "preferences": {},
                "redlines": [],
                "stop_decision": {"rules": []},
                "changelog": {"v0.5": ["refresh"]},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    runtime = compile_runtime_policy(source)

    assert runtime == tmp_path / "decision-policy.runtime.yaml"
    compiled = yaml.safe_load(runtime.read_text(encoding="utf-8"))
    assert compiled["runtime_schema_version"] == 1
    assert compiled["version"] == 0.5
    assert compiled["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert compiled["core_principles"][0]["statement"] == "先看证据。"
    rendered = runtime.read_text(encoding="utf-8")
    assert "evidence_turn_ids" not in rendered
    assert "derived_from" not in rendered
    assert "changelog" not in compiled
    assert "generated_at" not in compiled


def test_load_runtime_policy_rejects_stale_source(tmp_path: Path) -> None:
    source = tmp_path / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {"version": 0.5, "core_principles": [], "preferences": {}, "redlines": []},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    compile_runtime_policy(source)
    source.write_text(source.read_text(encoding="utf-8") + "gaps: []\n", encoding="utf-8")

    try:
        load_runtime_policy(source)
    except ValueError as exc:
        assert "stale" in str(exc)
        assert "compile-policy" in str(exc)
    else:
        raise AssertionError("expected stale runtime decision policy to fail")


def test_load_runtime_policy_rejects_tampered_runtime(tmp_path: Path) -> None:
    source = tmp_path / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {"version": 0.5, "core_principles": [], "preferences": {}, "redlines": []},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runtime = compile_runtime_policy(source)
    runtime.write_text(runtime.read_text(encoding="utf-8") + "unexpected: true\n", encoding="utf-8")

    try:
        load_runtime_policy(runtime)
    except ValueError as exc:
        assert "stale or modified" in str(exc)
    else:
        raise AssertionError("expected modified runtime decision policy to fail")


def test_compile_policy_cli_writes_persisted_runtime(tmp_path: Path) -> None:
    source = tmp_path / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {"version": 0.5, "core_principles": [], "preferences": {}, "redlines": []},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.main, ["compile-policy", "--source", str(source)])

    assert result.exit_code == 0
    assert str(tmp_path / "decision-policy.runtime.yaml") in result.output
    assert (tmp_path / "decision-policy.runtime.yaml").is_file()


def test_compile_policy_cli_rejects_non_runtime_output_name(tmp_path: Path) -> None:
    source = tmp_path / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {"version": 0.5, "core_principles": [], "preferences": {}, "redlines": []},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.main,
        ["compile-policy", "--source", str(source), "--out", str(tmp_path / "projection.yaml")],
    )

    assert result.exit_code != 0
    assert "must end with .runtime.yaml" in result.output


def test_reply_prompt_contains_the_persisted_runtime_without_dynamic_projection(tmp_path: Path) -> None:
    source = tmp_path / "decision-policy.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "version": 0.5,
                "core_principles": [
                    {
                        "id": f"principle-{index}",
                        "statement": f"statement-{index}",
                        "evidence_turn_ids": [f"turn-{index}"],
                    }
                    for index in range(10)
                ],
                "preferences": {},
                "redlines": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runtime = compile_runtime_policy(source)

    result = build_decision_reply(
        question="继续吗？",
        project=str(tmp_path),
        policy_path=runtime,
        examples_path=tmp_path / "missing.jsonl",
    )

    assert "runtime_schema_version: 1" in result["prompt"]
    assert "principle-9" in result["prompt"]
    assert "evidence_turn_ids" not in result["prompt"]


def test_direct_stop_prompt_contains_complete_persisted_runtime() -> None:
    policy = {
        "runtime_schema_version": 1,
        "source_sha256": "a" * 64,
        "core_principles": [{"id": "full-runtime-sentinel"}],
        "stop_decision": {"rules": [{"id": "stop-lightweight-decision"}]},
    }

    prompt = build_stop_decision_prompt(
        policy=policy,
        event={},
        message="要不要继续？",
        context="",
        project_path=Path("/tmp/project"),
    )

    assert '"decision_policy"' in prompt
    assert "runtime_schema_version" in prompt
    assert "full-runtime-sentinel" in prompt
