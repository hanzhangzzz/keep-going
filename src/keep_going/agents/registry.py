"""Agent name validation, resolution, and ``meta.json`` IO.

This module is the **single source of truth** for agent identity, per KTD-9 of
the multi-agent framework plan. Every entry point — ``keep-going agent new`` (CLI),
``keep-going start --agent`` (CLI), ``keep-going start --agents`` (CLI parses each),
``bridge.handle_stop_hook`` (defense-in-depth when reading ``state.json.agents``),
and the npm wrapper (defense-in-depth) — calls :func:`validate_agent_name`
here. Adding a new entry point without routing through this function is a bug.

Three responsibilities:
1. **Name validation** — :func:`validate_agent_name` enforces regex + reserved set.
2. **Resolution** — :func:`resolve_agent` returns the per-tier current decision policy path
   with project-tier shadowing global-tier.
3. **Meta IO** — :func:`load_meta` / :func:`save_meta` for the agent's
   ``meta.json``, with atomic write semantics.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# --- Constants (exported for shared use across CLI / bridge / npm) ---

# Agent names: leading lowercase letter, then 1-31 of [a-z0-9_-].
# Total accepted length: 2-32 chars (a 33-char name is the smallest to fail).
NAME_REGEX: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

# Reserved names: ``default`` (canonical decision policy binding per KTD-3), ``system``,
# Windows device names that would crash on Windows install paths, the
# parent-relative ``..`` / ``.``, and a placeholder for dunder-wrapped names
# (Python convention).
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "default",
        "system",
        "con",
        "prn",
        "aux",
        "nul",
        "..",
        ".",
    }
)

# Per-agent dir / file mode (POSIX only; security hygiene on shared
# workstations — see security F7 in the framework doc review). On POSIX
# hosts, ``mkdir(..., mode=0o700)`` and ``open(..., mode=0o600)`` ensure
# other local users cannot read the user's distilled decision policy. On Windows
# these are no-ops; we still set the values because callers reading the
# constant should see the documented intent.
AGENT_DIR_MODE: int = 0o700
POLICY_FILE_MODE: int = 0o600

# meta.json schema (KTD-11: minimal JSON, no new manifest files).
# ``current_policy`` points at the most recent ``policy-<ts>.yaml``. ``history``
# is an append-only list of prior policies (KTD-5). All fields are required
# except ``description`` (optional in ``agent new --description``) and
# ``history`` (empty for fresh agents).
AGENT_META_SCHEMA: dict[str, type] = {
    "name": str,
    "description": str,
    "created_at": str,  # ISO 8601
    "updated_at": str,  # ISO 8601
    "current_policy": str,  # absolute path
    "history": list,  # list of {"ts": str, "path": str}
}


# --- Public API ---


def validate_agent_name(name: object) -> dict[str, Any]:
    """Validate an agent name against the project's rules.

    Returns ``{"ok": True, "reason": None}`` if legal, or
    ``{"ok": False, "reason": "<explicit message>"}`` if illegal. Single
    source of truth — every entry point calls this.

    Rules (in evaluation order, first failure wins):
    1. Must be a non-empty string.
    2. Must not be in :data:`RESERVED_NAMES` (``default`` / ``system`` /
       Windows device names / ``..`` / ``.``).
    3. Must not start and end with ``__`` (Python dunder convention).
    4. Must not contain ``..`` (parent-relative injection guard).
    5. Must match :data:`NAME_REGEX` (lowercase letter, 1-31 of
       ``[a-z0-9_-]``).
    """
    if not isinstance(name, str):
        return {"ok": False, "reason": "name must be a string"}
    if name == "":
        return {"ok": False, "reason": "name is empty"}
    if name in RESERVED_NAMES:
        return {"ok": False, "reason": f"{name!r} is a reserved name"}
    if name.startswith("__") and name.endswith("__"):
        return {
            "ok": False,
            "reason": "names starting and ending with __ are reserved",
        }
    if ".." in name:
        return {"ok": False, "reason": "names containing '..' are reserved"}
    if not NAME_REGEX.match(name):
        return {
            "ok": False,
            "reason": (
                "name must match ^[a-z][a-z0-9_-]{1,31}$ "
                "(2-32 chars; lowercase, digits, '-', '_')"
            ),
        }
    return {"ok": True, "reason": None}


def _agent_root() -> Path:
    """Return the per-user global agents root, honoring ``KEEP_GOING_AGENTS_HOME``.

    Default is ``~/.keep-going/agents``. Override via env var ``KEEP_GOING_AGENTS_HOME``
    (used for tests; could be used for shared mounts, custom layouts).
    """
    override = os.environ.get("KEEP_GOING_AGENTS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".keep-going" / "agents"


def resolve_agent(
    name: str,
    project: str | None = None,
    canonical_policy: Path | None = None,
) -> dict[str, Any]:
    """Resolve an agent name to its current decision policy file path.

    Lookup order (per KTD-4):
    1. ``name == "default"`` → ``canonical_policy`` (always; the implicit agent).
    2. **project-tier**: ``<project>/.keep-going/agents/<name>/``.
    3. **global-tier**: ``~/.keep-going/agents/<name>/`` (or
       ``$KEEP_GOING_AGENTS_HOME/<name>/``).

    Returns ``{"path": Path, "scope": "project"|"global"|"canonical",
              "valid": True, "reason": None}`` on hit, or
            ``{"path": None, "scope": None, "valid": False,
              "reason": "<not_found|...>"}`` on miss.

    ``canonical_policy`` is the resolved path to
    ``artifacts/decision-policy.yaml`` from the project config; passed in by the
    caller (the bridge) to keep this module config-agnostic.
    """
    if name == "default":
        if canonical_policy is None:
            return {
                "path": None,
                "scope": None,
                "valid": False,
                "reason": "default agent requires canonical_policy argument",
            }
        return {
            "path": Path(canonical_policy).expanduser(),
            "scope": "canonical",
            "valid": True,
            "reason": None,
        }

    # Project-tier first (per KTD-4: project shadows global on collision).
    if project:
        project_agent = Path(project) / ".keep-going" / "agents" / name
        resolved = _try_resolve_tier(project_agent)
        if resolved is not None:
            return {
                "path": resolved,
                "scope": "project",
                "valid": True,
                "reason": None,
            }

    # Global-tier fallback.
    global_agent = _agent_root() / name
    resolved = _try_resolve_tier(global_agent)
    if resolved is not None:
        return {
            "path": resolved,
            "scope": "global",
            "valid": True,
            "reason": None,
        }

    return {
        "path": None,
        "scope": None,
        "valid": False,
        "reason": "not_found",
    }


def _try_resolve_tier(agent_dir: Path) -> Path | None:
    """Return the agent's current decision policy path if the agent dir is well-formed.

    An agent dir is "well-formed" when it exists, has a parseable
    ``meta.json``, and ``current_policy`` points at an existing file. Otherwise
    return ``None`` so the caller can fall through to the next tier.
    """
    if not agent_dir.is_dir():
        return None
    meta = load_meta(agent_dir)
    cd = meta.get("current_policy")
    if not isinstance(cd, str) or not cd:
        return None
    p = Path(cd)
    if not p.is_file():
        return None
    return p


def load_meta(agent_path: str | Path) -> dict[str, Any]:
    """Load an agent's ``meta.json``. Returns ``{}`` on miss / parse error.

    Missing or corrupt meta is **not** an error here — a freshly scaffolded
    agent has no meta until the first ``agent new`` or ``distill_for_agent``
    finishes. Callers that need a non-empty meta should check.
    """
    p = Path(agent_path) / "meta.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_meta(agent_path: str | Path, meta: dict[str, Any]) -> None:
    """Save an agent's ``meta.json`` atomically (write to ``.tmp`` then rename).

    The atomic write is a corruption guard for concurrent writers (e.g.,
    ``keep-going agent edit`` in one shell racing with ``keep-going distill-mine`` in
    another). POSIX ``rename`` is atomic on the same filesystem; on Windows
    we'd need ``os.replace`` (which is the same call under the hood).
    """
    p = Path(agent_path)
    p.mkdir(parents=True, exist_ok=True)
    # On POSIX, ``mkdir`` honors the process umask. Force AGENT_DIR_MODE so
    # the agent dir lands at 0o700 even if the umask is more permissive
    # (e.g., 022 on a shared host). On Windows ``chmod`` is a no-op for
    # read-only flags; we still call it so the intent is visible.
    try:
        p.chmod(AGENT_DIR_MODE)
    except OSError:
        # Windows or read-only fs: skip; the umask's default applies.
        pass

    tmp = p / "meta.json.tmp"
    tmp.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        tmp.chmod(POLICY_FILE_MODE)
    except OSError:
        pass
    # ``Path.replace`` is atomic on POSIX for same-fs renames. On Windows
    # it calls ``os.replace`` (also atomic).
    tmp.replace(p / "meta.json")


def list_agents(
    scope: str = "all",
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Enumerate agents without loading decision policy content.

    Returns a list of ``{name, scope, current_policy, updated_at}`` dicts. The
    ``current_policy`` field carries the path string from ``meta.json`` (may be
    stale; resolve before use). Names are deduplicated with project-tier
    precedence (per KTD-4).

    ``scope``: ``"all"`` (default) | ``"global"`` | ``"project"``. When
    ``scope="project"`` and ``project`` is ``None``, the result is ``[]``
    (lenient rather than error — the CLI layer can validate if needed).
    """
    if scope not in {"all", "global", "project"}:
        raise ValueError(f"scope must be one of all/global/project, got {scope!r}")

    by_name: dict[str, dict[str, Any]] = {}

    if scope in {"all", "project"} and project is not None:
        proj_dir = Path(project) / ".keep-going" / "agents"
        if proj_dir.is_dir():
            for child in sorted(proj_dir.iterdir()):
                if not child.is_dir():
                    continue
                meta = load_meta(child)
                by_name[child.name] = _entry("project", child.name, meta)

    if scope in {"all", "global"}:
        global_dir = _agent_root()
        if global_dir.is_dir():
            for child in sorted(global_dir.iterdir()):
                if not child.is_dir():
                    continue
                if child.name in by_name:
                    # Project-tier wins (already inserted).
                    continue
                meta = load_meta(child)
                by_name[child.name] = _entry("global", child.name, meta)

    return list(by_name.values())


def _entry(scope: str, name: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Build a single ``list_agents`` row from a meta.json dict."""
    return {
        "name": name,
        "scope": scope,
        "current_policy": meta.get("current_policy", ""),
        "updated_at": meta.get("updated_at", ""),
    }


def register_codex_agent(
    agent_name: str,
    policy_path: Path,
    *,
    codex_home: Path | None = None,
) -> Path:
    """Register a named agent as a Codex agent TOML file.

    Writes ``<codex_home>/agents/<agent_name>.toml``. If the file already
    exists, this is a no-op (idempotent per KTD-3).

    Builds the TOML dict programmatically (no template substitution) to
    prevent TOML injection — decision policy paths containing quotes or special
    characters cannot escape the string field.

    Returns the path to the written TOML file.
    """
    codex_target = codex_home or _default_codex_home()
    toml_path = codex_target / "agents" / f"{agent_name}.toml"

    if toml_path.exists():
        return toml_path

    instructions = _agent_instructions(agent_name, str(policy_path))
    data: dict[str, object] = {
        "name": agent_name,
        "description": f"Keep Going decision proxy: {agent_name}",
        "sandbox_mode": "workspace-write",
        "developer_instructions": instructions,
    }
    parsed_str = _toml_dumps(data)

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(parsed_str, encoding="utf-8")
    return toml_path


def _agent_instructions(agent_name: str, policy_path: str) -> str:
    """Build developer_instructions for a named agent TOML."""
    return (
        f"<identity>\n"
        f"You are Keep Going ({agent_name}), a local decision proxy for the user.\n"
        f"</identity>\n"
        f"\n"
        f"<scope>\n"
        f"Answer lightweight decision questions using the agent-specific decision policy at:\n"
        f"{policy_path}\n"
        f"</scope>\n"
        f"\n"
        f"<execution>\n"
        f"Call the repo-bound Keep Going runtime:\n"
        f"\n"
        f'```bash\n'
        f'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"\n'
        f'cd "$REPO_ROOT"\n'
        f"scripts/03-reply.sh --input-json\n"
        f"```\n"
        f"\n"
        f"Pass JSON on stdin:\n"
        f"\n"
        f'```json\n'
        f"{{\n"
        f'  "question": "<task agent question>",\n'
        f'  "project": "<task cwd>",\n'
        f'  "policy_path": "{policy_path}"\n'
        f"}}\n"
        f"```\n"
        f"</execution>\n"
        f"\n"
        f"<decision_policy>\n"
        f"- If `escalate=true`, stop and ask the real user.\n"
        f"- Never authorize commit, push, destructive operations, or secret handling.\n"
        f"- Prefer the shortest actionable reply.\n"
        f"</decision_policy>\n"
        f"\n"
        f"<output_contract>\n"
        f"Return:\n"
        f"- `reply`: direct message to the task agent\n"
        f"- `confidence`: numeric confidence\n"
        f"- `escalate`: whether to stop and ask the real user\n"
        f"- `evidence`: matched principles / heuristics\n"
        f"</output_contract>"
    )


def _default_codex_home() -> Path:
    """Return the default Codex home directory."""
    return Path.home() / ".codex"


def _toml_dumps(data: dict) -> str:
    """Minimal TOML serializer for agent TOML files.

    Only handles the flat key-value structure used by register_codex_agent.
    For string values with newlines, uses TOML multi-line basic strings.
    Control characters (except \\t, \\n, \\r) are replaced with \\uXXXX.
    """
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, str):
            if "\n" in value:
                escaped = _escape_toml_multiline(value)
                lines.append(f'{key} = """{escaped}"""')
            else:
                escaped = _escape_toml_inline(value)
                lines.append(f'{key} = "{escaped}"')
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _escape_toml_multiline(s: str) -> str:
    """Escape a string for TOML triple-quoted basic string.

    TOML allows \\t, \\n, \\r in multi-line basic strings. Everything else
    below 0x20 must be \\uXXXX-escaped. Backslashes must be doubled.
    """
    result: list[str] = []
    for c in s:
        if c == "\\":
            result.append("\\\\")
        elif c == "\n":
            result.append("\n")
        elif c == "\t":
            result.append("\t")
        elif c == "\r":
            result.append("\r")
        elif ord(c) < 0x20:
            result.append(f"\\u{ord(c):04x}")
        else:
            result.append(c)
    return "".join(result)


def _escape_toml_inline(s: str) -> str:
    """Escape a string for TOML single-line basic string."""
    result: list[str] = []
    for c in s:
        if c == "\\":
            result.append("\\\\")
        elif c == '"':
            result.append('\\"')
        elif c == "\t":
            result.append("\\t")
        elif c == "\r":
            result.append("\\r")
        elif c == "\n":
            result.append("\\n")
        elif ord(c) < 0x20:
            result.append(f"\\u{ord(c):04x}")
        else:
            result.append(c)
    return "".join(result)
