"""Tests for ``src/keep_going/agents/registry.py``.

Covers U1 of the multi-agent framework plan: name validation (including
reserved names + Windows device names + max-length), agent resolution
(project-tier shadows global; ``default`` always maps to canonical),
``meta.json`` IO atomicity, ``list_agents`` precedence and the no-decision policy-content
contract, plus the 0700/0600 permission hygiene on shared hosts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from keep_going.agents.registry import (
    AGENT_DIR_MODE,
    POLICY_FILE_MODE,
    NAME_REGEX,
    RESERVED_NAMES,
    list_agents,
    load_meta,
    resolve_agent,
    save_meta,
    validate_agent_name,
)


# --- validate_agent_name ---


def test_validate_accepts_kebab_case():
    """Canonical example from the plan: ``quality-reviewer``."""
    assert validate_agent_name("quality-reviewer") == {"ok": True, "reason": None}


def test_validate_accepts_snake_case():
    """Canonical example from the plan: ``e2e_tester``."""
    assert validate_agent_name("e2e_tester") == {"ok": True, "reason": None}


def test_validate_accepts_short():
    """Shortest legal name: 2 chars, ``a1``."""
    assert validate_agent_name("a1") == {"ok": True, "reason": None}


def test_validate_accepts_max_length():
    """32-char name: regex allows 2-32 chars; 32 is the max."""
    name = "a" + "b" * 31
    assert len(name) == 32
    assert validate_agent_name(name) == {"ok": True, "reason": None}


def test_validate_rejects_uppercase():
    res = validate_agent_name("Default")
    assert res["ok"] is False


def test_validate_rejects_leading_dash():
    res = validate_agent_name("-leading-dash")
    assert res["ok"] is False


def test_validate_rejects_parent_relative():
    res = validate_agent_name("../etc")
    assert res["ok"] is False
    assert ".." in res["reason"]


def test_validate_rejects_windows_device():
    for name in ("con", "prn", "aux", "nul"):
        res = validate_agent_name(name)
        assert res["ok"] is False, f"{name!r} should be rejected"


def test_validate_rejects_reserved_default():
    res = validate_agent_name("default")
    assert res["ok"] is False
    assert "default" in res["reason"]


def test_validate_rejects_dunder():
    res = validate_agent_name("__pycache__")
    assert res["ok"] is False


def test_validate_rejects_over_max_length():
    """33-char name: smallest that fails the {1,31} upper bound."""
    name = "a" + "b" * 32
    assert len(name) == 33
    res = validate_agent_name(name)
    assert res["ok"] is False


def test_validate_rejects_empty_string():
    res = validate_agent_name("")
    assert res["ok"] is False


def test_validate_rejects_non_string():
    assert validate_agent_name(None)["ok"] is False
    assert validate_agent_name(123)["ok"] is False
    assert validate_agent_name([])["ok"] is False


def test_validate_does_not_mutate_reserved_set():
    """``RESERVED_NAMES`` is a ``frozenset``; ``validate`` must not touch it."""
    before = set(RESERVED_NAMES)
    for n in ("default", "__anything__", "con", "../etc", "system", "nul"):
        validate_agent_name(n)
    assert set(RESERVED_NAMES) == before


# --- resolve_agent ---


def test_resolve_default_returns_canonical(tmp_path: Path):
    canonical = tmp_path / "decision-policy.yaml"
    canonical.write_text("core_principles: []\n", encoding="utf-8")
    res = resolve_agent("default", canonical_policy=canonical)
    assert res["valid"] is True
    assert res["scope"] == "canonical"
    assert res["path"] == canonical.expanduser()


def test_resolve_default_requires_canonical_policy():
    res = resolve_agent("default", canonical_policy=None)
    assert res["valid"] is False
    assert "canonical_policy" in res["reason"]


def test_resolve_default_ignores_project_tier_shadowing(tmp_path: Path):
    """``default`` is reserved and always resolves to canonical, even if a
    project-tier ``.keep-going/agents/default/`` dir exists."""
    canonical = tmp_path / "decision-policy.yaml"
    canonical.write_text("h: c\n", encoding="utf-8")
    proj_agent_dir = tmp_path / "project" / ".keep-going" / "agents" / "default"
    proj_agent_dir.mkdir(parents=True)
    (proj_agent_dir / "policy.yaml").write_text("h: p\n", encoding="utf-8")
    (proj_agent_dir / "meta.json").write_text(
        json.dumps({"current_policy": str(proj_agent_dir / "policy.yaml")}),
        encoding="utf-8",
    )
    res = resolve_agent("default", project=str(tmp_path / "project"), canonical_policy=canonical)
    assert res["scope"] == "canonical"
    assert res["path"] == canonical.expanduser()


def test_resolve_project_tier_shadows_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When both project-tier and global-tier exist, project wins (KTD-4)."""
    global_dir = tmp_path / "global-agents"
    global_dir.mkdir()
    g_agent = global_dir / "quality-reviewer"
    g_agent.mkdir(parents=True)
    g_policy = g_agent / "policy-2026.yaml"
    g_policy.write_text("h: g\n", encoding="utf-8")
    (g_agent / "meta.json").write_text(
        json.dumps({"current_policy": str(g_policy)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    project_dir = tmp_path / "project"
    p_agent = project_dir / ".keep-going" / "agents" / "quality-reviewer"
    p_policy = p_agent / "policy-2026.yaml"
    p_policy.parent.mkdir(parents=True)
    p_policy.write_text("h: p\n", encoding="utf-8")
    (p_agent / "meta.json").write_text(
        json.dumps({"current_policy": str(p_policy)}),
        encoding="utf-8",
    )

    res = resolve_agent("quality-reviewer", project=str(project_dir))
    assert res["valid"] is True
    assert res["scope"] == "project"
    assert res["path"] == p_policy


def test_resolve_falls_back_to_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No project-tier match → fall through to global."""
    global_dir = tmp_path / "global-agents"
    global_dir.mkdir()
    agent = global_dir / "e2e"
    policy = agent / "policy-x.yaml"
    policy.parent.mkdir(parents=True)
    policy.write_text("h: g\n", encoding="utf-8")
    (agent / "meta.json").write_text(
        json.dumps({"current_policy": str(policy)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    project_dir = tmp_path / "project"
    project_dir.mkdir()  # no agents
    res = resolve_agent("e2e", project=str(project_dir))
    assert res["valid"] is True
    assert res["scope"] == "global"


def test_resolve_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No project-tier, no global-tier → ``not_found``."""
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    res = resolve_agent("nonexistent", project=str(tmp_path / "proj"))
    assert res["valid"] is False
    assert res["reason"] == "not_found"


def test_resolve_meta_json_with_missing_policy_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If meta.json's ``current_policy`` points to a missing file, treat as
    'agent not present at this tier' and fall through."""
    global_dir = tmp_path / "g"
    agent = global_dir / "broken"
    agent.mkdir(parents=True)
    # meta.json points at a policy file that doesn't exist
    (agent / "meta.json").write_text(
        json.dumps({"current_policy": str(global_dir / "broken" / "policy-gone.yaml")}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    res = resolve_agent("broken")
    assert res["valid"] is False
    assert res["reason"] == "not_found"


# --- load_meta + save_meta ---


def test_save_and_load_roundtrip(tmp_path: Path):
    agent_dir = tmp_path / "agents" / "x"
    meta = {
        "name": "x",
        "current_policy": "/tmp/x/policy-2026.yaml",
        "updated_at": "2026-06-02T00:00:00+00:00",
        "history": [],
    }
    save_meta(agent_dir, meta)
    loaded = load_meta(agent_dir)
    assert loaded == meta


def test_save_meta_atomic_no_tmp_left(tmp_path: Path):
    """After ``save_meta``, no ``.tmp`` file remains in the agent dir.

    The atomic-rename pattern writes to ``meta.json.tmp`` then renames; the
    tmp file must be gone after a successful save.
    """
    agent_dir = tmp_path / "agents" / "x"
    save_meta(agent_dir, {"name": "x", "current_policy": "/x"})
    leftovers = list(agent_dir.glob("*.tmp"))
    assert leftovers == [], f"tmp files left behind: {leftovers}"


def test_save_meta_creates_dir_with_0700(tmp_path: Path):
    """Agent dir permissions on a shared host: 0o700."""
    if sys.platform.startswith("win"):
        pytest.skip("POSIX perms not enforced on Windows")
    agent_dir = tmp_path / "agents" / "x"
    save_meta(agent_dir, {"name": "x", "current_policy": "/x"})
    mode = agent_dir.stat().st_mode & 0o777
    assert mode == AGENT_DIR_MODE, f"expected {oct(AGENT_DIR_MODE)}, got {oct(mode)}"


def test_save_meta_meta_json_0600(tmp_path: Path):
    """``meta.json`` permissions: 0o600 (only owner can read)."""
    if sys.platform.startswith("win"):
        pytest.skip("POSIX perms not enforced on Windows")
    agent_dir = tmp_path / "agents" / "x"
    save_meta(agent_dir, {"name": "x", "current_policy": "/x"})
    meta = agent_dir / "meta.json"
    mode = meta.stat().st_mode & 0o777
    assert mode == POLICY_FILE_MODE, f"expected {oct(POLICY_FILE_MODE)}, got {oct(mode)}"


def test_load_meta_missing_dir_returns_empty(tmp_path: Path):
    assert load_meta(tmp_path / "nonexistent") == {}


def test_load_meta_corrupt_json_returns_empty(tmp_path: Path):
    agent_dir = tmp_path / "agents" / "x"
    agent_dir.mkdir(parents=True)
    (agent_dir / "meta.json").write_text("{not valid json", encoding="utf-8")
    assert load_meta(agent_dir) == {}


def test_load_meta_returns_empty_dict_on_missing_fields(tmp_path: Path):
    """Empty meta.json is OK (returns ``{}``); partial fields are kept."""
    agent_dir = tmp_path / "agents" / "x"
    agent_dir.mkdir(parents=True)
    (agent_dir / "meta.json").write_text("{}", encoding="utf-8")
    assert load_meta(agent_dir) == {}


# --- list_agents ---


def test_list_agents_returns_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``scope="all"`` returns the union of project-tier and global-tier."""
    global_dir = tmp_path / "g"
    global_dir.mkdir()
    _make_agent(global_dir / "g1", current_policy="/g1", updated_at="2026-06-01")
    _make_agent(global_dir / "g2", current_policy="/g2", updated_at="2026-06-02")
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    proj = tmp_path / "p"
    _make_agent(
        proj / ".keep-going" / "agents" / "p1",
        current_policy="/p1",
        updated_at="2026-06-03",
    )

    rows = list_agents(scope="all", project=str(proj))
    by_name = {r["name"]: r for r in rows}
    assert set(by_name.keys()) == {"g1", "g2", "p1"}
    assert by_name["g1"]["scope"] == "global"
    assert by_name["p1"]["scope"] == "project"


def test_list_agents_dedup_project_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the same name exists in both tiers, project-tier wins."""
    global_dir = tmp_path / "g"
    _make_agent(global_dir / "shared", current_policy="/global-shared", updated_at="G")
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    proj = tmp_path / "p"
    _make_agent(
        proj / ".keep-going" / "agents" / "shared",
        current_policy="/project-shared",
        updated_at="P",
    )

    rows = list_agents(scope="all", project=str(proj))
    shared = [r for r in rows if r["name"] == "shared"]
    assert len(shared) == 1
    assert shared[0]["scope"] == "project"
    assert shared[0]["current_policy"] == "/project-shared"


def test_list_agents_does_not_read_policy_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``list_agents`` reads ``meta.json`` only, not ``policy-*.yaml`` files.

    The contract: each row carries only the path string from meta, never
    any decision policy content. We assert this by stuffing sensitive-looking content
    into the decision policy file and confirming the row only exposes the path.
    """
    global_dir = tmp_path / "g"
    agent = global_dir / "a"
    agent.mkdir(parents=True)
    policy = agent / "policy-2026.yaml"
    policy.write_text("# sensitive decision policy content\n", encoding="utf-8")
    _make_agent(agent, current_policy=str(policy), updated_at="2026")
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    rows = list_agents(scope="global")
    assert len(rows) == 1
    assert rows[0]["name"] == "a"
    # The row carries only the four documented fields; the decision policy file's
    # content is not surfaced.
    assert set(rows[0].keys()) == {"name", "scope", "current_policy", "updated_at"}


def test_list_agents_scope_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    global_dir = tmp_path / "g"
    _make_agent(global_dir / "g1", current_policy="/g1", updated_at="2026-06-01")
    monkeypatch.setenv("KEEP_GOING_AGENTS_HOME", str(global_dir))

    proj = tmp_path / "p"
    _make_agent(
        proj / ".keep-going" / "agents" / "p1",
        current_policy="/p1",
        updated_at="2026-06-02",
    )

    g_only = list_agents(scope="global", project=str(proj))
    p_only = list_agents(scope="project", project=str(proj))
    assert {r["name"] for r in g_only} == {"g1"}
    assert {r["name"] for r in p_only} == {"p1"}


def test_list_agents_scope_project_without_project_returns_empty():
    """``scope="project"`` with no project arg returns ``[]`` (lenient)."""
    assert list_agents(scope="project", project=None) == []


def test_list_agents_invalid_scope_raises():
    with pytest.raises(ValueError, match="scope must be"):
        list_agents(scope="bogus")


# --- NAME_REGEX direct sanity ---


def test_name_regex_anchors_strictly():
    """Spot-check: regex must reject strings that fail any char-class
    requirement, not just the length boundary.

    Char class is ``[a-z0-9_-]`` only — trailing dash and dots/colons
    are NOT in the set, even though they may look "almost legal".
    """
    # Should reject
    assert not NAME_REGEX.match("A-quality")  # uppercase first
    assert not NAME_REGEX.match("-leading")   # leading dash
    assert not NAME_REGEX.match("with space")  # space
    assert not NAME_REGEX.match("with/slash")  # slash
    assert not NAME_REGEX.match("with.dot")    # dot is not in the charset
    assert not NAME_REGEX.match("with:colon")  # colon is not in the charset
    # Trailing dash IS legal (char class includes -), so the older test
    # that asserted the contrary was wrong. Confirm here that trailing dash
    # is legal but a single-char disallowed char is not.
    assert NAME_REGEX.match("trailing-")
    # Should accept
    assert NAME_REGEX.match("quality-reviewer")
    assert NAME_REGEX.match("e2e_tester")
    # Min length: regex requires 1 leading + at least 1 trailing char = 2 total.
    # Single-char ``"a"`` is REJECTED.
    assert not NAME_REGEX.match("a")
    assert NAME_REGEX.match("a" + "x" * 31)  # 32 chars (max)
    assert not NAME_REGEX.match("a" + "x" * 32)  # 33 chars (over max)


# --- helpers ---


def _make_agent(agent_dir: Path, current_policy: str, updated_at: str) -> None:
    """Create an agent dir with a populated ``meta.json`` for tests."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": agent_dir.name,
        "description": "",
        "created_at": updated_at,
        "updated_at": updated_at,
        "current_policy": current_policy,
        "history": [],
    }
    (agent_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
