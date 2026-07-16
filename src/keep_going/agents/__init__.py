"""Agent registry, distillation, and lifecycle for the multi-agent framework.

Public surface (per U1 of the multi-agent framework plan):
- ``validate_agent_name`` / ``resolve_agent`` / ``list_agents``: name validation,
  per-tier agent resolution, and enumeration.
- ``load_meta`` / ``save_meta``: agent ``meta.json`` IO with atomic writes.
- ``distill_for_agent`` / ``InsufficientCorpusError``: one-click distillation
  pipeline (U6, experimental).
- Constants ``NAME_REGEX`` / ``RESERVED_NAMES`` / ``AGENT_DIR_MODE`` / ``POLICY_FILE_MODE``
  / ``AGENT_META_SCHEMA`` are exported for shared use by every entry point
  (CLI / bridge / npm wrapper — KTD-9).

See ``docs/plans/2026-06-02-001-feat-keep-going-multi-agent-framework-plan.md`` for
the framework plan.
"""

from __future__ import annotations

from .distill import InsufficientCorpusError, distill_for_agent
from .registry import (
    AGENT_DIR_MODE,
    AGENT_META_SCHEMA,
    POLICY_FILE_MODE,
    NAME_REGEX,
    RESERVED_NAMES,
    list_agents,
    load_meta,
    resolve_agent,
    save_meta,
    validate_agent_name,
)

__all__ = [
    "AGENT_DIR_MODE",
    "AGENT_META_SCHEMA",
    "POLICY_FILE_MODE",
    "InsufficientCorpusError",
    "NAME_REGEX",
    "RESERVED_NAMES",
    "distill_for_agent",
    "list_agents",
    "load_meta",
    "resolve_agent",
    "save_meta",
    "validate_agent_name",
]
