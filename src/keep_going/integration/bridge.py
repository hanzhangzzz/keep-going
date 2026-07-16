"""Project-level Stop hook bridge for Keep Going."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keep_going.agents.registry import resolve_agent, validate_agent_name
from keep_going.config import Config
from keep_going.decision.policy_runtime import runtime_policy_path
from keep_going.integration.stop_context import build_stop_decision_context
from keep_going.decision.stop_decision import decide_stop
from keep_going.decision.stop_safety import enforce_stop_safety_policy, event_message_texts


STATE_VERSION = 2
DEFAULT_BACKEND = "cli"
DEFAULT_FORCE_SKILL = "keep-going"
HOSTS = {"claude-code", "codex", "generic"}
BACKENDS = {"direct", "cli"}
INPUT_MODES = {"stdin", "append-arg"}
PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "go.mod", "Cargo.toml")
FANOUT_MAX_WORKERS = 4
# Max consecutive Keep Going-driven continuations after one human turn before the
# Stop hook hands control back to the human, even if the Keep Going would still block.
# Bounds runaway AI↔Keep Going loops; override with KEEP_GOING_STOP_MAX_CHAIN_DEPTH.
DEFAULT_MAX_CHAIN_DEPTH = 8


def enable_project(
    project: Path | str | None = None,
    *,
    host: str = "claude-code",
    backend: str = DEFAULT_BACKEND,
    command: str | None = None,
    shell: bool = False,
    input_mode: str = "stdin",
    force_skill: str = DEFAULT_FORCE_SKILL,
    shell_executable: str | None = None,
    state_home: Path | None = None,
    agents: list[str] | None = None,
    render_mode: str = "block",
    force: bool = False,
) -> dict[str, Any]:
    project_path = resolve_project(project)
    if command and backend == "direct":
        backend = "cli"
    if backend == "cli" and not str(command or "").strip():
        command = default_cli_command(host)
    _validate_backend(backend, command=command, input_mode=input_mode, host=host)
    resolved_agents = agents if agents is not None else ["default"]
    state = _load_state(project_path, state_home=state_home)
    state = _ensure_v2(state)
    if (
        state.get("enabled")
        and resolved_agents != state.get("agents", ["default"])
        and not force
    ):
        raise ValueError(
            f"agent list changed from {state.get('agents')} to {resolved_agents}; "
            "use --force to switch agents on an enabled project"
        )
    state.update(
        {
            "version": STATE_VERSION,
            "enabled": True,
            "host": host,
            "backend": backend,
            "command": command or "",
            "shell": bool(shell),
            "input_mode": input_mode,
            "force_skill": force_skill,
            "shell_executable": shell_executable or "",
            "agents": resolved_agents,
            "render_mode": render_mode,
            "updated_at": _now_iso(),
        }
    )
    _write_state(project_path, state, state_home=state_home)
    return status_project(project_path, state_home=state_home)


def disable_project(project: Path | str | None = None, *, state_home: Path | None = None) -> dict[str, Any]:
    project_path = resolve_project(project)
    state = _load_state(project_path, state_home=state_home)
    state.update({"enabled": False, "updated_at": _now_iso()})
    _write_state(project_path, state, state_home=state_home)
    return status_project(project_path, state_home=state_home)


def status_project(project: Path | str | None = None, *, state_home: Path | None = None) -> dict[str, Any]:
    project_path = resolve_project(project)
    state = _load_state(project_path, state_home=state_home)
    if state.get("enabled"):
        host = str(state.get("host") or "claude-code")
        backend, command = _effective_stop_backend(
            backend=str(state.get("backend") or DEFAULT_BACKEND),
            command=str(state.get("command") or ""),
            host=host,
            input_mode=str(state.get("input_mode") or "stdin"),
        )
        state["backend"] = backend
        state["command"] = command
    state["project"] = str(project_path)
    state["project_id"] = project_id(project_path)
    state["state_file"] = str(_state_file(project_path, state_home=state_home))
    return state


def handle_stop_hook(
    cfg: Config,
    event: dict[str, Any],
    *,
    host: str | None = None,
    state_home: Path | None = None,
    top_k: int = 5,
    generate: bool = False,
    record_metrics: bool = True,
) -> dict[str, Any]:
    # ``stop_hook_active`` means the host is already continuing because a prior
    # Stop hook blocked. The old code short-circuited to allow here, which made
    # the Keep Going unable to ever chain two replies in a row (it could substitute
    # for the human on every *other* turn only). We now keep the flag and defer
    # the loop-safety decision to a bounded, context-aware check below so the
    # Keep Going can carry multi-round flows (e.g. grill-me) and hand back to the
    # human on its own semantic judgement or a depth cap — not blindly.
    stop_hook_active = event.get("stop_hook_active") is True

    project_path = resolve_project(_event_project(event))
    state = status_project(project_path, state_home=state_home)
    state = _ensure_v2(state)
    resolved_host = host or str(state.get("host") or infer_host(event))
    if resolved_host not in HOSTS:
        raise ValueError(f"unsupported host: {resolved_host}")

    backend = str(state.get("backend") or DEFAULT_BACKEND)
    command = str(state.get("command") or "")
    if state.get("enabled"):
        backend, command = _effective_stop_backend(
            backend=backend,
            command=command,
            host=resolved_host,
            input_mode=str(state.get("input_mode") or "stdin"),
        )

    agents = state.get("agents") or ["default"]
    render_mode = str(state.get("render_mode") or "advisory")

    base: dict[str, Any] = {
        "enabled": bool(state.get("enabled")),
        "project": str(project_path),
        "project_id": state["project_id"],
        "host": resolved_host,
        "backend": backend,
        "state_file": state["state_file"],
    }

    if not state.get("enabled"):
        return _record_stop_hook_metric(
            {**base, "action": "allow", "reason": "project disabled", "host_response": None, "decision_result": None},
            event=event,
            state_home=state_home,
            record_metrics=record_metrics,
        )

    if not agents:
        return _record_stop_hook_metric(
            {**base, "action": "allow", "reason": "agents_empty", "host_response": None, "decision_result": None},
            event=event,
            state_home=state_home,
            record_metrics=record_metrics,
        )

    adapted = {**event, "project": str(project_path)}
    adapted.setdefault("cwd", str(project_path))
    adapted["decision_context"] = build_stop_decision_context(
        adapted,
        project_path=project_path,
        cache_root=_stop_context_cache_root(project_path, state_home=state_home),
    )

    decision_context = adapted["decision_context"] if isinstance(adapted["decision_context"], dict) else {}
    chain_depth = int(decision_context.get("continuation_chain_depth") or 0)
    base["continuation_chain_depth"] = chain_depth
    base["stop_hook_active"] = stop_hook_active

    if stop_hook_active:
        max_depth = _max_chain_depth()
        if chain_depth >= max_depth:
            return _record_stop_hook_metric(
                {
                    **base,
                    "action": "allow",
                    "reason": f"stop_chain_depth_exceeded:{chain_depth}/{max_depth}",
                    "host_response": None,
                    "decision_result": None,
                },
                event=event,
                state_home=state_home,
                record_metrics=record_metrics,
            )
        confidence = str(decision_context.get("context_confidence") or "")
        status_kind = str(decision_context.get("context_status") or "")
        if confidence == "low" or status_kind in {"missing_transcript", "read_error"}:
            return _record_stop_hook_metric(
                {
                    **base,
                    "action": "allow",
                    "reason": "stop_hook_active_low_confidence",
                    "host_response": None,
                    "decision_result": None,
                },
                event=event,
                state_home=state_home,
                record_metrics=record_metrics,
            )

    decision_kwargs: dict[str, Any] = {
        "project_path": project_path,
        "top_k": top_k,
        "generate": generate,
        "backend": backend,
        "command": command,
        "shell": bool(state.get("shell")),
        "input_mode": str(state.get("input_mode") or "stdin"),
        "force_skill": str(state.get("force_skill") or DEFAULT_FORCE_SKILL),
        "shell_executable": str(state.get("shell_executable") or ""),
    }

    if len(agents) == 1:
        decision_result = _invoke_single_agent(
            cfg, adapted, agents[0], project_path, decision_kwargs
        )
    else:
        for name in agents:
            validation = validate_agent_name(name)
            if not validation.get("ok"):
                return _record_stop_hook_metric(
                    {
                        **base,
                        "action": "allow",
                        "reason": f"agent_name_invalid:{name}",
                        "host_response": None,
                        "decision_result": {
                            "action": "escalate",
                            "reply": "",
                            "reason": f"agent_name_invalid:{name}",
                            "confidence": 0.0,
                            "evidence": [],
                        },
                    },
                    event=event,
                    state_home=state_home,
                    record_metrics=record_metrics,
                )
        decision_result = _fanout_dispatch(cfg, adapted, agents, project_path, decision_kwargs)

    decision_result = enforce_stop_safety_policy(
        decision_result,
        decision_context,
        max_chain_depth=_max_chain_depth(),
        current_event=event,
    )
    action = _normalized_keep_going_action(decision_result)

    if action == "escalate":
        sectioned = _sectioned_system_message(decision_result, agents) if len(agents) >= 2 else None
        return _record_stop_hook_metric(
            {
                **base,
                "action": "allow",
                "reason": str(decision_result.get("reason") or "keep-going escalated to human"),
                "host_response": None,
                "decision_result": decision_result,
                **({"_sectioned_message": sectioned} if sectioned else {}),
            },
            event=event,
            state_home=state_home,
            record_metrics=record_metrics,
        )

    effective_action = _effective_action(action, render_mode=render_mode)

    if effective_action == "allow":
        sectioned = _sectioned_system_message(decision_result, agents) if len(agents) >= 2 else None
        return _record_stop_hook_metric(
            {
                **base,
                "action": "allow",
                "reason": str(decision_result.get("reason") or "keep-going allowed stop"),
                "host_response": None,
                "decision_result": decision_result,
                **({"_sectioned_message": sectioned} if sectioned else {}),
            },
            event=event,
            state_home=state_home,
            record_metrics=record_metrics,
        )

    if not str(decision_result.get("reply") or "").strip():
        return _record_stop_hook_metric(
            {
                **base,
                "action": "allow",
                "reason": "keep-going block decision had empty reply",
                "host_response": None,
                "decision_result": decision_result,
            },
            event=event,
            state_home=state_home,
            record_metrics=record_metrics,
        )

    host_response = _host_block_response(decision_result, host=resolved_host)
    return _record_stop_hook_metric(
        {
            **base,
            "action": "block",
            "reason": "keep-going reply injected through stop hook",
            "host_response": host_response,
            "decision_result": decision_result,
        },
        event=event,
        state_home=state_home,
        record_metrics=record_metrics,
    )


def _invoke_single_agent(
    cfg: Config,
    adapted_event: dict[str, Any],
    agent_name: str,
    project_path: Path,
    decision_kwargs: dict[str, Any],
) -> dict[str, Any]:
    policy_path = _resolve_agent_policy(agent_name, project_path, cfg)
    try:
        return decide_stop(cfg, adapted_event, policy_path=policy_path, **decision_kwargs)
    except Exception as exc:
        return _cli_failure_decision(exc, command=str(decision_kwargs.get("command") or ""))


def _fanout_dispatch(
    cfg: Config,
    adapted_event: dict[str, Any],
    agents: list[str],
    project_path: Path,
    decision_kwargs: dict[str, Any],
) -> dict[str, Any]:
    per_agent_results: list[dict[str, Any]] = []
    max_workers = min(len(agents), FANOUT_MAX_WORKERS)

    def _call_agent(agent_name: str) -> dict[str, Any]:
        policy_path = _resolve_agent_policy(agent_name, project_path, cfg)
        try:
            return decide_stop(cfg, adapted_event, policy_path=policy_path, **decision_kwargs)
        except Exception:
            return {
                "action": "escalate",
                "reply": "",
                "reason": f"agent_{agent_name}_backend_failed",
                "confidence": 0.0,
                "evidence": [{"source": "fanout", "id": agent_name, "kind": "agent_error"}],
            }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_call_agent, name): name for name in agents}
        for future in as_completed(futures):
            try:
                result = future.result(timeout=300)
            except Exception:
                result = {
                    "action": "escalate",
                    "reply": "",
                    "reason": f"agent_{futures[future]}_timeout",
                    "confidence": 0.0,
                    "evidence": [{"source": "fanout", "id": futures[future], "kind": "agent_timeout"}],
                }
            per_agent_results.append(result)

    ordered: list[dict[str, Any]] = []
    for name in agents:
        for result in per_agent_results:
            if name in str(result.get("reason") or "") or name in str(result.get("evidence") or ""):
                ordered.append(result)
                break
    if len(ordered) != len(agents):
        ordered = per_agent_results

    return merge_stop_decisions(ordered, agent_names=agents)


def _resolve_agent_policy(agent_name: str, project_path: Path, cfg: Config) -> Path | None:
    if agent_name == "default":
        return runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml")
    resolved = resolve_agent(agent_name, project=str(project_path))
    if resolved.get("valid") and resolved.get("policy_path"):
        return Path(resolved["policy_path"])
    return runtime_policy_path(cfg.paths.artifacts_dir / "decision-policy.yaml")


def _effective_action(action: str, *, render_mode: str) -> str:
    if render_mode == "block":
        return action
    return "allow"


def _sectioned_system_message(decision_result: dict[str, Any], agents: list[str]) -> str | None:
    evidence = decision_result.get("evidence", [])
    if not evidence:
        return None
    sections: list[str] = []
    for name in agents:
        agent_evs = [e for e in evidence if isinstance(e, dict) and e.get("agent") == name]
        if agent_evs:
            sections.append(f"## {name} (action={decision_result.get('action')}, conf={decision_result.get('confidence')})\n{decision_result.get('reply', '')}")
    return "\n\n".join(sections) if sections else None


def render_stop_hook_output(result: dict[str, Any]) -> str:
    response = result.get("host_response")
    if result.get("action") == "block" and isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False) + "\n"
    if result.get("action") == "allow":
        allow_response = _host_allow_response(result)
        if allow_response:
            return json.dumps(allow_response, ensure_ascii=False) + "\n"
    return ""


def _record_stop_hook_metric(
    result: dict[str, Any],
    *,
    event: dict[str, Any],
    state_home: Path | None,
    record_metrics: bool,
) -> dict[str, Any]:
    if not record_metrics:
        result["metrics_recorded"] = False
        return result
    result["metrics_recorded"] = True
    decision_result = result.get("decision_result") if isinstance(result.get("decision_result"), dict) else {}
    question = str(decision_result.get("question") or _event_message(event) or "")
    reply = str(decision_result.get("reply") or "")
    keep_going_action = _normalized_keep_going_action(decision_result) if decision_result else str(result.get("action") or "allow")
    row = {
        "ts": _now_iso(),
        "event_type": "stop_hook",
        "project": result.get("project"),
        "project_id": result.get("project_id"),
        "host": result.get("host"),
        "backend": result.get("backend"),
        "enabled": result.get("enabled"),
        "action": result.get("action"),
        "reason": result.get("reason"),
        "hook_event_name": str(event.get("hook_event_name") or event.get("event_name") or event.get("event") or ""),
        "continuation_injected": keep_going_action == "block",
        "escalate": keep_going_action == "escalate",
        "confidence": decision_result.get("confidence"),
        "category": str(decision_result.get("category") or ""),
        "continuation_chain_depth": result.get("continuation_chain_depth"),
        "stop_hook_active": event.get("stop_hook_active") is True,
        "question_sha1": hashlib.sha1(question.encode("utf-8")).hexdigest() if question else "",
        "reply_sha1": hashlib.sha1(reply.encode("utf-8")).hexdigest() if reply else "",
    }
    try:
        path = _stop_hook_events_file(state_home=state_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return result


def run_self_test(
    cfg: Config,
    *,
    project: Path | str | None = None,
    host: str = "codex",
    state_home: Path | None = None,
) -> dict[str, Any]:
    if host not in HOSTS:
        raise ValueError(f"unsupported host: {host}")
    cleanup: tempfile.TemporaryDirectory[str] | None = None
    if state_home is None:
        cleanup = tempfile.TemporaryDirectory(prefix="keep-going-bridge-")
        state_home = Path(cleanup.name)
    try:
        project_path = resolve_project(project or cfg.root)
        transcript_path = _write_self_test_transcript(state_home, project_path)
        event = {
            "hook_event_name": "Stop",
            "cwd": str(project_path),
            "session_id": "keep-going-self-test",
            "transcript_path": str(transcript_path),
            "last_assistant_message": "我已经完成修改。要不要继续做最终验证？",
        }
        disabled = handle_stop_hook(cfg, event, host=host, state_home=state_home)
        enabled_state = enable_project(project_path, host=host, state_home=state_home)
        enabled = handle_stop_hook(cfg, event, host=host, state_home=state_home)
        disable_project(project_path, state_home=state_home)
        passed = (
            disabled.get("action") == "allow"
            and enabled_state.get("enabled") is True
            and enabled.get("action") == "block"
            and (enabled.get("host_response") or {}).get("decision") == "block"
        )
        return {
            "passed": passed,
            "project": str(project_path),
            "host": host,
            "state_home": str(state_home),
            "disabled_action": disabled.get("action"),
            "enabled_action": enabled.get("action"),
            "host_response": enabled.get("host_response"),
        }
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def _write_self_test_transcript(state_home: Path, project_path: Path) -> Path:
    path = state_home / "self-test-transcript.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "keep-going-self-test", "cwd": str(project_path)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": "请完成当前修改，并在结束前做最终验证。"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "我已经完成修改。要不要继续做最终验证？"}],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def infer_host(event: dict[str, Any]) -> str:
    raw = " ".join(str(event.get(key) or "") for key in ("host", "source", "tool", "hook_event_name"))
    lowered = raw.lower()
    if "codex" in lowered:
        return "codex"
    if "claude" in lowered or "stop" in lowered:
        return "claude-code"
    return "generic"


def default_cli_command(host: str) -> str:
    if host == "codex":
        override = os.environ.get("KEEP_GOING_CODEX_CLI_COMMAND")
        if override:
            return override
        executable = shutil.which("codex") or "codex"
        return shlex.join(
            [
                executable,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "-",
            ]
        )
    if host == "claude-code":
        override = os.environ.get("KEEP_GOING_CLAUDE_CODE_CLI_COMMAND")
        if override:
            return override
        executable = shutil.which("claude") or "claude"
        return shlex.join(
            [
                executable,
                "-p",
                "--no-session-persistence",
                "--permission-mode",
                "default",
                "--tools",
                "",
            ]
        )
    override = os.environ.get("KEEP_GOING_GENERIC_CLI_COMMAND")
    if override:
        return override
    return default_cli_command("codex")


def _effective_stop_backend(*, backend: str, command: str, host: str, input_mode: str) -> tuple[str, str]:
    if backend == "direct" and not os.environ.get("KEEP_GOING_ALLOW_DIRECT_STOP_HOOK"):
        backend = "cli"
        command = default_cli_command(host)
    if backend == "cli" and not command.strip():
        command = default_cli_command(host)
    _validate_backend(backend, command=command, input_mode=input_mode, host=host)
    return backend, command


def resolve_project(project: Path | str | None = None) -> Path:
    raw = str(project or os.getcwd()).strip() or os.getcwd()
    path = Path(raw).expanduser().resolve(strict=False)
    if path.is_file():
        path = path.parent
    return _workspace_root(path)


def project_id(project: Path | str) -> str:
    path = resolve_project(project)
    canonical = str(path)
    try:
        canonical = str(path.resolve(strict=True))
    except FileNotFoundError:
        pass
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name or "workspace").strip("-") or "workspace"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def state_root(state_home: Path | None = None) -> Path:
    if state_home is not None:
        return state_home.expanduser().resolve(strict=False)
    raw = os.environ.get("KEEP_GOING_STATE_HOME")
    if raw:
        return Path(raw).expanduser().resolve(strict=False)
    return Path.home() / ".keep-going" / "projects"


def _stop_hook_events_file(*, state_home: Path | None = None) -> Path:
    raw = os.environ.get("KEEP_GOING_EVENTS_HOME")
    if raw:
        return Path(raw).expanduser().resolve(strict=False) / "stop-hook.jsonl"
    if state_home is not None:
        return state_home.expanduser().resolve(strict=False).parent / "events" / "stop-hook.jsonl"
    return Path.home() / ".keep-going" / "events" / "stop-hook.jsonl"


def _host_block_response(decision_result: dict[str, Any], *, host: str) -> dict[str, str]:
    reply = str(decision_result.get("reply") or "").strip()
    reason = "\n".join(
        [
            "Keep Going 已按项目级 Stop hook 代用户给出轻量决策：",
            reply,
            "",
            "请把上面内容当作用户回复继续处理；如果后续触及提交、推送、删除、生产或密钥等高风险动作，仍需真人确认。",
        ]
    ).strip()
    if host in {"claude-code", "codex", "generic"}:
        return {"decision": "block", "reason": reason}
    raise ValueError(f"unsupported host: {host}")


def _host_allow_response(result: dict[str, Any]) -> dict[str, Any] | None:
    if not result.get("enabled"):
        return None
    decision_result = result.get("decision_result")
    if not isinstance(decision_result, dict):
        return None
    message = _allow_status_message(result, decision_result)
    return {
        "systemMessage": message,
    }


def _allow_status_message(result: dict[str, Any], decision_result: dict[str, Any]) -> str:
    reason = str(result.get("reason") or decision_result.get("reason") or "keep-going allowed stop").strip()
    if len(reason) > 80:
        reason = reason[:77].rstrip() + "..."
    confidence = decision_result.get("confidence")
    confidence_text = f", confidence={confidence:.2f}" if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else ""
    if _normalized_keep_going_action(decision_result) == "escalate":
        return f"Keep Going Stop hook: allow, needs human next ({reason}{confidence_text})"
    return f"Keep Going Stop hook: allow ({reason}{confidence_text})"


def _normalized_keep_going_action(decision_result: dict[str, Any]) -> str:
    action = str(decision_result.get("action") or "").strip().lower()
    if action in {"allow", "block", "escalate"}:
        return action
    raise ValueError("Stop decision action must be allow, block, or escalate")


def _cli_failure_decision(exc: Exception, *, command: str) -> dict[str, Any]:
    detail = " ".join(str(exc).split())
    if len(detail) > 500:
        detail = detail[:497].rstrip() + "..."
    return {
        "action": "escalate",
        "reply": "",
        "reason": "cli_backend_failed",
        "confidence": 0.0,
        "category": "",
        "evidence": [
            {
                "source": "cli",
                "id": type(exc).__name__,
                "kind": "stop_decision_backend_error",
                "command": command,
                "detail": detail,
            }
        ],
    }


def _event_message(event: dict[str, Any]) -> str:
    values = event_message_texts(event)
    return values[0] if values else ""


def _event_context(event: dict[str, Any]) -> str:
    parts = []
    for key in ("hook_event_name", "event_name", "transcript_path", "session_id", "cwd"):
        value = str(event.get(key) or "").strip()
        if value:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


def _event_project(event: dict[str, Any]) -> str:
    for key in ("project", "cwd", "workspace", "repo"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return os.getcwd()


def _workspace_root(path: Path) -> Path:
    candidates = (path, *path.parents)
    for candidate in candidates:
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return path


def _state_file(project: Path, *, state_home: Path | None) -> Path:
    return state_root(state_home) / project_id(project) / "state.json"


def _stop_context_cache_root(project: Path, *, state_home: Path | None) -> Path:
    return _state_file(project, state_home=state_home).parent / "stop-context"


def _load_state(project: Path, *, state_home: Path | None) -> dict[str, Any]:
    path = _state_file(project, state_home=state_home)
    state = _default_state(project)
    if not path.exists():
        return state
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid bridge state JSON: {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"bridge state must be an object: {path}")
    state.update(loaded)
    return state


def _write_state(project: Path, state: dict[str, Any], *, state_home: Path | None) -> None:
    path = _state_file(project, state_home=state_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _default_state(project: Path) -> dict[str, Any]:
    timestamp = _now_iso()
    return {
        "version": STATE_VERSION,
        "enabled": False,
        "project": str(project),
        "project_id": project_id(project),
        "host": "claude-code",
        "backend": DEFAULT_BACKEND,
        "command": default_cli_command("claude-code"),
        "shell": False,
        "input_mode": "stdin",
        "force_skill": DEFAULT_FORCE_SKILL,
        "shell_executable": "",
        "agents": ["default"],
        "render_mode": "block",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _ensure_v2(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("version", 1) >= 2:
        state.setdefault("agents", ["default"])
        state.setdefault("render_mode", "block")
        return state
    state["version"] = 2
    state.setdefault("agents", ["default"])
    state.setdefault("render_mode", "block")
    return state


def merge_stop_decisions(results: list[dict[str, Any]], *, agent_names: list[str] | None = None) -> dict[str, Any]:
    """Merge per-agent stop decisions into a single decision.

    Priority: escalate > block > allow. Within the winning tier, reason/reply
    come from the highest-confidence result. All evidence is concatenated.
    """
    if not results:
        return {
            "action": "allow",
            "reply": "",
            "reason": "agents_empty",
            "confidence": 0.0,
            "evidence": [],
        }

    names = agent_names or [f"agent-{i}" for i in range(len(results))]

    escalates = [
        (i, r) for i, r in enumerate(results) if r.get("action") == "escalate"
    ]
    if escalates:
        best = max(escalates, key=lambda pair: pair[1].get("confidence", 0.0))
        return {
            "action": "escalate",
            "reply": best[1].get("reply", ""),
            "reason": best[1].get("reason", "merged_escalate"),
            "confidence": best[1].get("confidence", 0.0),
            "category": best[1].get("category", ""),
            "evidence": _merge_evidence(results, names),
        }

    blocks = [(i, r) for i, r in enumerate(results) if r.get("action") == "block"]
    if blocks:
        best = max(blocks, key=lambda pair: pair[1].get("confidence", 0.0))
        return {
            "action": "block",
            "reply": best[1].get("reply", ""),
            "reason": best[1].get("reason", "merged_block"),
            "confidence": best[1].get("confidence", 0.0),
            "category": best[1].get("category", ""),
            "evidence": _merge_evidence(results, names),
        }

    best_all = max(results, key=lambda r: r.get("confidence", 0.0))
    return {
        "action": "allow",
        "reply": best_all.get("reply", ""),
        "reason": best_all.get("reason", "merged_allow"),
        "confidence": best_all.get("confidence", 0.0),
        "category": best_all.get("category", ""),
        "evidence": _merge_evidence(results, names),
    }


def _merge_evidence(results: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for name, result in zip(names, results):
        for ev in result.get("evidence", []):
            entry = dict(ev) if isinstance(ev, dict) else {"value": ev}
            entry["agent"] = name
            merged.append(entry)
    return merged


def _validate_backend(backend: str, *, command: str | None, input_mode: str, host: str) -> None:
    if backend not in BACKENDS:
        raise ValueError(f"unsupported backend: {backend}")
    if input_mode not in INPUT_MODES:
        raise ValueError(f"unsupported input_mode: {input_mode}")
    if host not in HOSTS:
        raise ValueError(f"unsupported host: {host}")
    if backend == "cli" and not str(command or "").strip():
        raise ValueError("cli backend requires --command")


def _max_chain_depth() -> int:
    raw = os.environ.get("KEEP_GOING_STOP_MAX_CHAIN_DEPTH", "").strip()
    if not raw:
        return DEFAULT_MAX_CHAIN_DEPTH
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CHAIN_DEPTH
    return value if value >= 0 else DEFAULT_MAX_CHAIN_DEPTH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
