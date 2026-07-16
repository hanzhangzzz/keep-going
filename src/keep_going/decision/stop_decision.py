"""Unified Stop-hook decision interface."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from keep_going.config import Config
from keep_going.decision.policy_runtime import runtime_policy_path
from keep_going.decision.reply import generate_reply_with_anthropic, load_decision_policy


STOP_ACTIONS = {"allow", "block", "escalate"}
STOP_CATEGORIES = {"preference", "verification", "authorization", "capability", "information", "other"}
INPUT_MODES = {"stdin", "append-arg"}
STOP_DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["allow", "block", "escalate"]},
        "reply": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "category": {
            "type": "string",
            "enum": ["preference", "verification", "authorization", "capability", "information", "other"],
        },
    },
    "required": ["action", "reply", "reason", "confidence", "evidence", "category"],
    "additionalProperties": False,
}


def decide_stop(
    cfg: Config,
    event: dict[str, Any],
    *,
    project_path: Path,
    top_k: int = 5,
    generate: bool = False,
    generator: Callable[[str, str], str] | None = None,
    backend: str = "direct",
    command: str = "",
    shell: bool = False,
    input_mode: str = "stdin",
    force_skill: str = "keep-going",
    shell_executable: str = "",
    policy_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the Stop decision schema.

    The bridge owns host adaptation. This function owns the single Stop decision
    surface and returns only the structured decision the bridge may adapt.

    ``policy_path`` (U2 of the multi-agent framework plan): when ``None``
    (default), falls back to the canonical decision policy at
    ``cfg.paths.artifacts_dir / "decision-policy.yaml"`` — preserving today's
    single-decision policy callers. When given (as a string or :class:`Path`), that decision policy
    is loaded instead. This parameterization is the load-bearing change that
    lets the bridge's fan-out in U3 load per-agent policies.
    """
    del top_k
    if policy_path is None:
        policy_path = runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml")
    policy_path = Path(policy_path).expanduser()

    if backend == "cli":
        return _decide_with_cli(
            cfg,
            event,
            project_path=project_path,
            command=command,
            shell=shell,
            input_mode=input_mode,
            force_skill=force_skill,
            shell_executable=shell_executable,
            policy_path=policy_path,
        )
    if backend != "direct":
        raise ValueError(f"unsupported Stop decision backend: {backend}")

    policy = load_decision_policy(policy_path)
    message = _event_message(event)
    context = _event_context(event)
    rules = _stop_decision_rules(policy)

    if generate:
        prompt = build_stop_decision_prompt(
            policy=policy,
            event=event,
            message=message,
            context=context,
            project_path=project_path,
        )
        raw = (generator or generate_reply_with_anthropic)(prompt, cfg.models.decision)
        parsed = _parse_json_object(raw)
        return normalize_stop_decision(
            parsed,
            fallback_reason="generated_stop_decision",
            fallback_evidence=[
                {
                    "source": "stop_decision_prompt",
                    "id": "generated_stop_decision",
                    "kind": "generated_decision",
                }
            ],
        )

    decision = _decide_from_rules(message, rules, policy_path=policy_path)
    return normalize_stop_decision(decision, fallback_reason="stop_decision_schema_normalized")


def _decide_with_cli(
    cfg: Config,
    event: dict[str, Any],
    *,
    project_path: Path,
    command: str,
    shell: bool,
    input_mode: str,
    force_skill: str,
    shell_executable: str,
    policy_path: Path,
) -> dict[str, Any]:
    if not command.strip():
        raise ValueError("cli Stop decision backend requires command")
    policy = load_decision_policy(policy_path)
    prompt = _external_cli_prompt(event, project_path=project_path, force_skill=force_skill, policy=policy, policy_path=policy_path)
    stdout = _run_command(
        command,
        prompt=prompt,
        cwd=project_path,
        shell=shell,
        input_mode=input_mode,
        shell_executable=shell_executable,
    )
    text = stdout.strip()
    if not text:
        raise RuntimeError("Keep Going cli backend returned empty output")
    parsed = _parse_json_object(text)
    return normalize_stop_decision(
        parsed,
        fallback_reason="cli_backend",
        fallback_evidence=[{"source": "cli", "id": "structured_cli_backend", "kind": "stop_decision_backend"}],
    )


def _run_command(
    command: str,
    *,
    prompt: str,
    cwd: Path,
    shell: bool,
    input_mode: str,
    shell_executable: str,
) -> str:
    if input_mode not in INPUT_MODES:
        raise ValueError(f"unsupported input_mode: {input_mode}")

    if shell:
        executable = shell_executable or os.environ.get("SHELL") or "/bin/zsh"
        rendered_command = command if input_mode == "stdin" else f"{command} {shlex.quote(prompt)}"
        args: str | list[str] = [executable, "-lc", rendered_command]
        input_text = prompt if input_mode == "stdin" else None
    else:
        args = shlex.split(command)
        if not args:
            raise ValueError("cli backend command is empty")
        if input_mode == "append-arg":
            args.append(prompt)
            input_text = None
        else:
            input_text = prompt

    env = os.environ.copy()
    env["KEEP_GOING_STOP_HOOK_SUPPRESS"] = "1"
    env["KEEP_GOING_STOP_CLI_ACTIVE"] = "1"
    schema_file: Path | None = None
    if not shell and isinstance(args, list):
        args, schema_file = _add_structured_output_schema(args)
    timeout_seconds = _cli_timeout_seconds()
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or f"Keep Going cli backend failed with exit code {result.returncode}")
        return result.stdout
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Keep Going cli backend timed out after {timeout_seconds:g} seconds") from exc
    finally:
        if schema_file is not None:
            try:
                schema_file.unlink()
            except OSError:
                pass


def _cli_timeout_seconds() -> float:
    raw = os.environ.get("KEEP_GOING_STOP_CLI_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 300.0
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError("KEEP_GOING_STOP_CLI_TIMEOUT_SECONDS must be a number") from exc
    if timeout <= 0:
        raise ValueError("KEEP_GOING_STOP_CLI_TIMEOUT_SECONDS must be greater than 0")
    return timeout


def _add_structured_output_schema(args: list[str]) -> tuple[list[str], Path | None]:
    if _looks_like_codex_exec(args) and "--output-schema" not in args:
        schema_file = _write_schema_file()
        insert_at = len(args) - 1 if args and args[-1] == "-" else len(args)
        return [*args[:insert_at], "--output-schema", str(schema_file), *args[insert_at:]], schema_file
    if _looks_like_claude_print(args) and "--json-schema" not in args:
        return [*args, "--json-schema", json.dumps(STOP_DECISION_JSON_SCHEMA, ensure_ascii=False)], None
    return args, None


def _looks_like_codex_exec(args: list[str]) -> bool:
    if len(args) < 2:
        return False
    executable = Path(args[0]).name
    return executable == "codex" and args[1] in {"exec", "e"}


def _looks_like_claude_print(args: list[str]) -> bool:
    if not args:
        return False
    executable = Path(args[0]).name
    return executable == "claude" and any(arg in {"-p", "--print"} for arg in args[1:])


def _write_schema_file() -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", prefix="keep-going-stop-schema-", delete=False)
    with handle:
        json.dump(STOP_DECISION_JSON_SCHEMA, handle, ensure_ascii=False)
        handle.write("\n")
    return Path(handle.name)


def _external_cli_prompt(
    event: dict[str, Any],
    *,
    project_path: Path,
    force_skill: str,
    policy: dict[str, Any],
    policy_path: Path,
) -> str:
    payload = {
        "project": str(project_path),
        "assistant_message": _event_message(event),
        "recent_context": _event_context(event),
        "decision_context": _event_decision_context(event),
        "raw_event_minimal": _raw_event_minimal(event),
        "decision_policy_path": str(policy_path),
        "decision_policy": policy,
    }
    return "\n".join(
        [
            "你是本机 Keep Going bridge 的执行后端。",
            "你必须以用户复刻体的身份做判断，目标是把真人从低风险循环决策中解放出来。",
            f"必须优先调用或遵循 `{force_skill}` skill/plugin 的用户决策 decision policy；本 prompt 已内联 decision_policy。",
            "任务：基于本次 session 的 assistant 输出，先分诊、再决定当前 Stop hook 应该 allow、block 还是 escalate。",
            "",
            "第一步·分诊。判断「AI 此刻为什么需要人」，归入五类之一（输出到 category 字段）：",
            "- preference：偏好/拍板类轻量决策（继续、选方案、范围收束、优先级取舍）→ 你的主场，用 decision policy + 用户先例代答（block）。",
            "- verification：AI 声称完成/修好，但 decision_context.verification_state 不是 passed，或结论缺证据 → 不要放行结束，代答要求先跑验证并贴结果，reply 里给出具体验证动作（block）。",
            "- authorization：涉及提交、推送、删除、生产、密钥、付款等不可逆或高风险授权 → 必须 escalate，不能代答。",
            "- capability：AI 卡住、重复同一步骤、原地空转、无新进展 → 按收敛性判断：一句纠偏能推回正轨就 block，否则 allow 或 escalate 止损。",
            "- information：需要只有真人掌握的世界事实（环境观察结果、业务背景、外部系统状态）→ escalate；绝对禁止编造事实代答。",
            "分诊拿不准时归 other，并倾向 escalate。",
            "",
            "第二步·判断与输出。只输出一个 JSON 对象，不要输出 Markdown 或额外解释。",
            'JSON schema: {"action": "allow|block|escalate", "reply": string, "reason": string, "confidence": number, "evidence": array, "category": "preference|verification|authorization|capability|information|other"}',
            "语义：allow=结束本次 Stop；block=把 reply 注入上游 agent 继续执行；escalate=需要真人，不能代答。",
            "校准优先于代答率：confidence 必须反映真实把握，宁可 escalate 也不要高置信答错；confidence 低于 0.6 时不要 block。",
            "不要把 decision_policy 当作机械规则表逐条匹配；你要按用户画像、偏好、边界和当前上下文综合判断。",
            "先识别 decision policy ai_collaboration_modes 中的当前协作模式：co-explorer（用户在探讨想法）时不要替用户催执行，探讨类问题交回真人；executor（方案已对齐）时才放手代答推进。",
            "block 的 reply 要像用户本人说话：中文、短、结论先行、可直接执行，遵循 decision policy vocabulary 的口吻；给方向时同时给理由。",
            "优先参考 decision_context 中的 latest_user_goal、explicit_constraints、verification_state、pending_question 和 risk_flags。",
            "如果 decision_context.context_confidence 为 low 或 missing_transcript，不要凭空替用户做具体推进决策；优先 allow 或 escalate。",
            "decision_context.continuation_chain_depth 表示本轮人类发言后 Keep Going 已连续代答的次数；次数越高越要倾向收敛、交回真人。",
            "允许跨多轮连续 block 推进（例如逐题 grill、连续文档往返），前提是 AI 有实质进展，且确实存在你能替用户拍板的轻量决策点。",
            "输入事件：",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        ]
    )


def normalize_stop_decision(
    raw: dict[str, Any],
    *,
    fallback_reason: str,
    fallback_evidence: list[Any] | None = None,
) -> dict[str, Any]:
    """Normalize any decision backend output into the Stop decision schema."""
    if not isinstance(raw, dict):
        raise ValueError("Stop decision must be a JSON object")

    action = _normalized_action(raw)
    reason = str(raw.get("reason") or fallback_reason).strip() or fallback_reason
    return {
        "action": action,
        "reply": str(raw.get("reply") or ""),
        "reason": reason,
        "confidence": _confidence(raw.get("confidence")),
        "evidence": _evidence(raw.get("evidence"), fallback=fallback_evidence),
        "category": _category(raw.get("category")),
    }


def build_stop_decision_prompt(
    *,
    policy: dict[str, Any],
    event: dict[str, Any],
    message: str,
    context: str,
    project_path: Path,
) -> str:
    payload = {
        "project": str(project_path),
        "message": message,
        "context": context,
        "decision_context": _event_decision_context(event),
        "raw_event_minimal": _raw_event_minimal(event),
        "decision_policy": policy,
    }
    return "\n".join(
        [
            "你是用户的 Keep Going Stop decision。",
            "任务：先分诊（preference=偏好拍板 / verification=验证缺失 / authorization=不可逆授权 / capability=空转 / information=需要真人的世界事实 / other），再判断当前 enabled Stop hook 事件应该 allow、block 还是 escalate。",
            "只输出一个 JSON 对象，不要输出 Markdown 或额外解释。",
            'JSON schema: {"action":"allow|block|escalate","reply":string,"reason":string,"confidence":number,"evidence":array,"category":string}',
            "语义：allow=结束本次 Stop；block=把 reply 注入上游 agent 继续执行；escalate=需要真人，不能代答。",
            "authorization 与 information 类必须 escalate；confidence 低于 0.6 不要 block。",
            "如果输出 block，reply 必须是可直接当作用户回复继续执行的中文短句。",
            "输入：",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


def _decide_from_rules(message: str, rules: list[dict[str, Any]], *, policy_path: Path) -> dict[str, Any]:
    conditional: list[dict[str, Any]] = []
    defaults: list[dict[str, Any]] = []
    for rule in rules:
        if _has_condition(rule):
            conditional.append(rule)
        else:
            defaults.append(rule)

    for rule in conditional:
        matched = _match_rule(rule, message)
        if matched:
            return _rule_decision(rule, policy_path=policy_path, matched=matched)

    if defaults:
        return _rule_decision(defaults[0], policy_path=policy_path, matched=["default"])
    return {
        "action": "allow",
        "reply": "",
        "reason": "stop_decision_no_matching_rule",
        "confidence": 0.5,
        "evidence": [
            {
                "source": str(policy_path),
                "id": "stop_decision_no_matching_rule",
                "kind": "schema_default",
            }
        ],
    }


def _stop_decision_rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    section = policy.get("stop_decision") or policy.get("stop_hook_decision") or {}
    rows = section.get("rules") if isinstance(section, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _rule_decision(rule: dict[str, Any], *, policy_path: Path, matched: list[str]) -> dict[str, Any]:
    rule_id = str(rule.get("id") or "stop_decision_rule").strip() or "stop_decision_rule"
    return {
        "action": str(rule.get("action") or "allow").strip().lower() or "allow",
        "reply": str(rule.get("reply") or ""),
        "reason": str(rule.get("reason") or rule_id),
        "confidence": rule.get("confidence", 0.8),
        "category": _category(rule.get("category")),
        "evidence": [
            {
                "source": str(policy_path),
                "id": rule_id,
                "kind": "stop_decision_rule",
                "matched": matched,
                "why": str(rule.get("why") or ""),
            }
        ],
    }


def _has_condition(rule: dict[str, Any]) -> bool:
    when = rule.get("when")
    if isinstance(when, dict) and when:
        return True
    return any(
        key in rule
        for key in (
            "markers",
            "completion_markers",
            "terms",
            "patterns",
            "contains_any",
            "contains_all",
            "regex_any",
        )
    )


def _match_rule(rule: dict[str, Any], message: str) -> list[str]:
    matched: list[str] = []
    when = rule.get("when")
    if isinstance(when, dict):
        if when.get("message_empty") is True:
            return ["message_empty"] if not message.strip() else []
        if "contains_any" in when:
            hit = _first_contains(message, _as_str_list(when.get("contains_any")))
            if not hit:
                return []
            matched.append(f"contains_any:{hit}")
        if "contains_all" in when:
            missing = [item for item in _as_str_list(when.get("contains_all")) if item.lower() not in message.lower()]
            if missing:
                return []
            matched.append("contains_all")
        if "regex_any" in when:
            hit = _first_regex(message, _as_str_list(when.get("regex_any")))
            if not hit:
                return []
            matched.append(f"regex_any:{hit}")
        if when.get("completion_report") is True:
            completion = _match_completion_report(message, rule)
            if not completion:
                return []
            matched.extend(completion)

    marker = _first_contains(message, _as_str_list(rule.get("markers")))
    if marker:
        matched.append(f"markers:{marker}")

    completion = _match_completion_report(message, rule)
    if completion:
        matched.extend(completion)

    term = _first_contains(message, _as_str_list(rule.get("terms") or rule.get("contains_any")))
    if term:
        matched.append(f"terms:{term}")

    pattern = _first_regex(message, _as_str_list(rule.get("patterns") or rule.get("regex_any")))
    if pattern:
        matched.append(f"patterns:{pattern}")

    return matched


def _match_completion_report(message: str, rule: dict[str, Any]) -> list[str]:
    markers = _as_str_list(rule.get("completion_markers"))
    if not markers:
        return []
    no_pending = _as_str_list(rule.get("no_pending_markers"))
    marker_count = sum(1 for marker in markers if marker in message)
    min_markers = _as_int(rule.get("min_completion_markers"), default=3)
    if marker_count >= min_markers:
        return [f"completion_markers:{marker_count}"]
    hit = _first_contains(message, no_pending)
    if marker_count >= 1 and hit:
        return [f"completion_markers:{marker_count}", f"no_pending:{hit}"]
    return []


def _normalized_action(raw: dict[str, Any]) -> str:
    action = str(raw.get("action") or "").strip().lower()
    if action in STOP_ACTIONS:
        return action
    raise ValueError("Stop decision action must be allow, block, or escalate")


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    parsed = float(value)
    if not math.isfinite(parsed):
        return 0.0
    return round(max(0.0, min(parsed, 1.0)), 2)


def _category(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in STOP_CATEGORIES else ""


def _evidence(value: Any, *, fallback: list[Any] | None) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if value:
        return [value]
    return fallback or []


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if not stripped:
        raise ValueError("Stop decision backend returned empty output")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Stop decision backend returned no JSON object")
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Stop decision backend JSON must be an object")
    return parsed


def _event_message(event: dict[str, Any]) -> str:
    for key in ("question", "message"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    for key in ("last_assistant_message", "assistant_message", "assistant_response", "rawOutput"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return ""


def _event_context(event: dict[str, Any]) -> str:
    parts = []
    for key in ("hook_event_name", "event_name", "transcript_path", "session_id", "cwd"):
        value = str(event.get(key) or "").strip()
        if value:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


def _event_decision_context(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("decision_context")
    return value if isinstance(value, dict) else {}


def _raw_event_minimal(event: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "hook_event_name",
        "event_name",
        "session_id",
        "transcript_path",
        "cwd",
        "project",
        "question",
        "message",
        "last_assistant_message",
        "assistant_message",
        "assistant_response",
        "rawOutput",
    ):
        value = event.get(key)
        if value is not None and str(value).strip():
            result[key] = str(value)
    extra_keys = sorted(str(key) for key in event if key not in result and key != "decision_context")
    if extra_keys:
        result["extra_keys"] = extra_keys
    return result


def _first_contains(text: str, candidates: list[str]) -> str:
    lowered = text.lower()
    for item in candidates:
        if item.lower() in lowered:
            return item
    return ""


def _first_regex(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return pattern
    return ""


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    stripped = str(value).strip()
    return [stripped] if stripped else []


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
