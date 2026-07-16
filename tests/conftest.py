from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from keep_going.decision.policy_runtime import compile_runtime_policy


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def bootstrap_public_test_policy() -> Iterator[None]:
    canonical = ROOT / "artifacts" / "decision-policy.yaml"
    runtime = ROOT / "artifacts" / "decision-policy.runtime.yaml"
    created = not canonical.exists()
    if created:
        shutil.copy2(ROOT / "artifacts" / "decision-policy.template.yaml", canonical)
        compile_runtime_policy(canonical)
    try:
        yield
    finally:
        if created:
            canonical.unlink(missing_ok=True)
            runtime.unlink(missing_ok=True)
