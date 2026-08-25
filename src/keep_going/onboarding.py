"""One-command personal decision-policy onboarding from local agent sessions."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .corpus.adapters import claude_code, codex
from .corpus.classify import classify_text
from .corpus.harvest import should_keep_turn
from .corpus.schema import Turn
from .corpus.scrub import scrub
from .decision.policy_runtime import compile_runtime_policy
from .integration.bridge import default_cli_command

PolicyGenerator = Callable[[str, str], dict[str, Any] | str]

_DECISION_LABELS = frozenset(
    {
        "execute-short",
        "rejection",
        "scope-correction",
        "scope-expansion",
        "tool-evaluation",
        "strategy-meta",
        "verification-demand",
        "evidence-probe",
        "writing-style",
        "visual-design",
        "ai-collab-meta",
        "interrupt-rollback",
        "failure-mode",
        "delivery-finalize",
        "meta-self-reflection",
        "choice-among-options",
    }
)


def onboard_personal_dna(
    cfg: Config,
    *,
    project: Path,
    host: str,
    max_sessions: int = 5,
    max_turns: int = 40,
    window_days: int | None = None,
    scope: str = "recent",
    replace: bool = False,
    generator: PolicyGenerator | None = None,
) -> dict[str, Any]:
    """Distill selected sessions into canonical + persisted runtime policy."""
    _validate_options(host, max_sessions, max_turns, scope)
    source_path = cfg.paths.artifacts_dir / "decision-policy.yaml"
    if source_path.exists() and not replace:
        raise FileExistsError(f"personal DNA already exists: {source_path}; review it, then rerun with --replace")
    selected, session_count = _select_turns(
        cfg,
        project=project,
        max_sessions=max_sessions,
        max_turns=max_turns,
        window_days=window_days,
        scope=scope,
        host=host,
    )
    if len(selected) < 3:
        raise RuntimeError(
            f"not enough decision-bearing user turns: {len(selected)} (minimum 3); "
            "use --scope recent, increase --window-days, or complete a few agent sessions first"
        )

    evidence_path = _write_evidence_bundle(cfg, selected, session_count, scope)
    prompt = _build_distillation_prompt(selected)
    raw_policy = (generator or generate_policy_with_host_cli)(prompt, host)
    personal = _parse_policy_result(raw_policy)
    policy = _build_canonical_policy(cfg, personal, selected, window_days or cfg.window.days, host)
    _write_canonical(source_path, policy, replace=replace)
    runtime_path = compile_runtime_policy(source_path)

    return {
        "status": "success",
        "summary": f"Personal DNA distilled from {session_count} sessions and {len(selected)} decision turns.",
        "profile_summary": policy["profile_summary"],
        "selection": {"sessions": session_count, "turns": len(selected), "scope": scope, "host": host},
        "artifacts": {
            "source_policy": str(source_path),
            "runtime_policy": str(runtime_path),
            "evidence_bundle": str(evidence_path),
        },
        "next_actions": [
            "Review the persisted source and runtime policies.",
            "Enable Keep Going for a project and run its self-test.",
        ],
    }


def generate_policy_with_host_cli(prompt: str, host: str) -> dict[str, Any]:
    command = os.environ.get("KEEP_GOING_DISTILL_COMMAND", "").strip() or default_cli_command(host)
    args = shlex.split(command)
    if not args:
        raise RuntimeError("distillation CLI command is empty")
    env = os.environ.copy()
    env["KEEP_GOING_STOP_HOOK_SUPPRESS"] = "1"
    env["KEEP_GOING_STOP_CLI_ACTIVE"] = "1"
    try:
        result = subprocess.run(
            args,
            cwd=Path.cwd(),
            input=prompt,
            text=True,
            capture_output=True,
            timeout=600,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{host} CLI not found; install and authenticate it, then retry") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("personal DNA distillation timed out after 600 seconds; retry with fewer sessions") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"personal DNA distillation failed: {detail[:1200]}")
    return _extract_json_object(result.stdout)


def _validate_options(host: str, max_sessions: int, max_turns: int, scope: str) -> None:
    if host not in {"claude-code", "codex"}:
        raise ValueError("host must be claude-code or codex")
    if max_sessions < 1 or max_turns < 3:
        raise ValueError("max_sessions must be >= 1 and max_turns must be >= 3")
    if scope not in {"recent", "project"}:
        raise ValueError("scope must be recent or project")


def _select_turns(
    cfg: Config,
    *,
    project: Path,
    max_sessions: int,
    max_turns: int,
    window_days: int | None,
    scope: str,
    host: str,
) -> tuple[list[dict[str, Any]], int]:
    since = datetime.now(timezone.utc) - timedelta(days=window_days or cfg.window.days)
    grouped: dict[tuple[str, str], list[Turn]] = defaultdict(list)
    seen_turn_ids: set[str] = set()
    seen_content: set[str] = set()
    for turn in _iter_turns(cfg, since, host):
        if turn.role != "user" or turn.ts < since or not should_keep_turn(turn, cfg):
            continue
        if scope == "project" and not _same_project(turn.project, project):
            continue
        labels = classify_text(turn.content)
        if not _DECISION_LABELS.intersection(labels):
            continue
        if turn.turn_id in seen_turn_ids or turn.content in seen_content:
            continue
        seen_turn_ids.add(turn.turn_id)
        seen_content.add(turn.content)
        grouped[(turn.source, turn.session_id)].append(turn)
    sessions = sorted(grouped.values(), key=lambda rows: max(row.ts for row in rows), reverse=True)[:max_sessions]
    candidates = [_selection_row(turn, cfg, classify_text(turn.content)) for rows in sessions for turn in rows]
    candidates.sort(key=lambda row: (row["decision_score"], row["ts"]), reverse=True)
    return candidates[:max_turns], len(sessions)


def _iter_turns(cfg: Config, since: datetime, host: str):
    if host == "claude-code":
        yield from claude_code.iter_sessions(cfg.sources.claude_code_dir, since=since)
        return
    yield from codex.iter_sessions(cfg.sources.codex_archived_dir, since=since)
    current = cfg.sources.codex_sessions_dir
    if current is not None and current != cfg.sources.codex_archived_dir:
        yield from codex.iter_sessions(current, since=since)


def _same_project(raw: str, project: Path) -> bool:
    try:
        return Path(raw).expanduser().resolve(strict=False) == project.expanduser().resolve(strict=False)
    except OSError:
        return False


def _selection_row(turn: Turn, cfg: Config, labels: list[str]) -> dict[str, Any]:
    score = len(_DECISION_LABELS.intersection(labels)) * 10 + min(len(turn.content), 1000) / 1000
    repl = cfg.scrub.user_replacement
    return {
        "turn_id": turn.turn_id,
        "source": turn.source,
        "session_ref": f"session-{hashlib.sha256(turn.session_id.encode('utf-8')).hexdigest()[:8]}",
        "ts": turn.ts.isoformat(),
        "labels": labels,
        "decision_score": round(score, 3),
        "assistant_context": scrub((turn.prev_assistant or "")[:1200], user_replacement=repl),
        "user_decision": scrub(turn.content[:2000], user_replacement=repl),
    }


def _write_evidence_bundle(
    cfg: Config, selected: list[dict[str, Any]], session_count: int, scope: str
) -> Path:
    path = cfg.paths.data_dir / "onboarding" / "latest-selection.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = {"sessions": session_count, "turns": len(selected), "scope": scope, "selected": selected}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _build_distillation_prompt(selected: list[dict[str, Any]]) -> str:
    evidence = json.dumps(selected, ensure_ascii=False, indent=2)
    return "\n".join(
        [
            "You distill a user's personal decision DNA from their scrubbed agent-session decisions.",
            "Infer only recurring preferences supported by evidence. Do not copy private content verbatim.",
            "Every principle or heuristic must cite only turn_id values present below.",
            "Return one JSON object only, with these keys: profile_summary, core_principles,",
            "preferences, heuristics, vocabulary, strategic_frame, ai_collaboration_modes.",
            "Use concise statements in the user's dominant language. Prefer 3-8 high-signal items per section.",
            "Do not invent authorization. Sensitive, irreversible, secret, production, payment, deletion,",
            "commit, push, and fact-dependent information decisions must remain human-only.",
            "Object items use id plus statement/gate/pref/trigger/reply_hint/rule as appropriate,",
            "and evidence_turn_ids as an array. preferences is an object grouped by domain.",
            "Evidence:",
            evidence,
        ]
    )


def _parse_policy_result(raw: dict[str, Any] | str) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else _extract_json_object(raw)
    required = {"profile_summary", "core_principles", "preferences"}
    missing = sorted(required.difference(data))
    if missing:
        raise ValueError(f"distilled policy missing required fields: {', '.join(missing)}")
    if not isinstance(data.get("core_principles"), list) or not data["core_principles"]:
        raise ValueError("distilled policy must contain at least one core principle")
    return data


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    candidates = [stripped]
    if "```" in stripped:
        candidates.extend(part.strip().removeprefix("json").strip() for part in stripped.split("```")[1::2])
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("distillation backend did not return a valid JSON object")


def _build_canonical_policy(
    cfg: Config,
    personal: dict[str, Any],
    selected: list[dict[str, Any]],
    window_days: int,
    host: str,
) -> dict[str, Any]:
    baseline = _load_baseline(cfg)
    evidence_ids = {row["turn_id"] for row in selected}
    normalized = _sanitize_evidence(personal, evidence_ids)
    policy = {
        "version": 1.0,
        "status": "canonical",
        "distill_mode": "session-llm",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile_summary": _profile_summary(normalized["profile_summary"]),
        "window": {"days": window_days, "sessions": len({row["session_ref"] for row in selected})},
        "core_principles": _merge_by_id(baseline["core_principles"], normalized["core_principles"]),
        "current_state_gates": baseline["current_state_gates"],
        "preferences": normalized.get("preferences", {}),
        "heuristics": normalized.get("heuristics", []),
        "stop_decision": baseline["stop_decision"],
        "vocabulary": normalized.get("vocabulary", {}),
        "strategic_frame": normalized.get("strategic_frame", {}),
        "ai_collaboration_modes": normalized.get("ai_collaboration_modes", []),
        "redlines": baseline["redlines"],
        "gaps": ["Policy was distilled from a bounded session sample; review before broadening autonomy."],
        "changelog": [{"version": 1.0, "change": f"Personal DNA cold-started through {host}."}],
    }
    return _scrub_tree(policy, cfg.scrub.user_replacement)


def _load_baseline(cfg: Config) -> dict[str, Any]:
    candidates = [
        cfg.paths.artifacts_dir / "decision-policy.template.yaml",
        Path(__file__).resolve().parents[2] / "artifacts" / "decision-policy.template.yaml",
    ]
    for path in candidates:
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    raise FileNotFoundError("public decision-policy template not found")


def _sanitize_evidence(value: Any, allowed: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                [item for item in item_value if item in allowed]
                if key == "evidence_turn_ids"
                else _sanitize_evidence(item_value, allowed)
            )
            for key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_evidence(item, allowed) for item in value]
    return value


def _merge_by_id(primary: Any, secondary: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [
        *(primary if isinstance(primary, list) else []),
        *(secondary if isinstance(secondary, list) else []),
    ]:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        rows.append(item)
    return rows


def _profile_summary(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("summary", "statement", "description"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
    if isinstance(value, list):
        statements = [
            str(item.get("statement") or item.get("summary") or "").strip()
            for item in value
            if isinstance(item, dict)
        ]
        joined = " ".join(text for text in statements if text)
        if joined:
            return joined
    raise ValueError("distilled profile_summary must be text or contain summary statements")


def _scrub_tree(value: Any, replacement: str) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_tree(item, replacement) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_tree(item, replacement) for item in value]
    if isinstance(value, str):
        return scrub(value, user_replacement=replacement)
    return value


def _write_canonical(path: Path, policy: dict[str, Any], *, replace: bool) -> None:
    if path.exists() and not replace:
        raise FileExistsError(f"personal DNA already exists: {path}; review it, then rerun with --replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak")
        backup.write_bytes(path.read_bytes())
        backup.chmod(0o600)
    rendered = yaml.safe_dump(policy, allow_unicode=True, sort_keys=False)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(rendered, encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)
    path.chmod(0o600)
