from __future__ import annotations

import os
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tracked_tree_respects_public_privacy_boundary() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "privacy-audit.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "privacy audit: PASS" in result.stdout


def test_reviewed_concept_svg_matches_exact_manifest() -> None:
    from keep_going.privacy import REVIEWED_MEDIA_SHA256, reviewed_media_violations

    path = "docs/assets/keep-going-concept.svg"
    data = (ROOT / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == REVIEWED_MEDIA_SHA256[path]
    assert reviewed_media_violations(path, data) == []
    assert reviewed_media_violations(path, data + b" ") == ["reviewed media hash mismatch"]


def test_private_decision_artifacts_are_rejected_generically() -> None:
    from keep_going.privacy import path_violations

    assert path_violations("artifacts/decision-policy.template.yaml") == []
    assert path_violations("artifacts/decision-policy.yaml") == ["private decision policy artifact"]
    assert path_violations("artifacts/decision-private.yaml") == ["private decision policy artifact"]
    assert path_violations("artifacts/decision-policy.yaml.bak") == ["private decision policy artifact"]
    assert path_violations("artifacts/.decision-policy.yaml.tmp") == ["private decision policy artifact"]


def test_history_audit_accepts_clean_root_and_rejects_private_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Keep Going",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Keep Going",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "KEEP_GOING_PRIVACY_ROOT": str(repo),
    }
    subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=repo, check=True, env=env)

    clean = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "privacy-audit.py"), "--history"],
        cwd=repo,
        text=True,
        capture_output=True,
        env=env,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr

    (repo / "slide-private.png").write_bytes(b"not-an-image")
    subprocess.run(["git", "add", "slide-private.png"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "leak"], cwd=repo, check=True, env=env)
    leaked = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "privacy-audit.py"), "--history"],
        cwd=repo,
        text=True,
        capture_output=True,
        env=env,
    )
    assert leaked.returncode != 0
    assert "binary/media artifact" in leaked.stdout


def test_audit_reads_staged_index_blob_not_safe_worktree(tmp_path: Path) -> None:
    repo, env = _clean_repo(tmp_path)
    readme = repo / "README.md"
    readme.write_text("private home: /" + "Users/alice/work\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    readme.write_text("public\n", encoding="utf-8")

    result = _audit(repo, env)

    assert result.returncode != 0
    assert "index:README.md: absolute user-home path" in result.stdout


def test_history_checks_every_commit_path_for_reused_blob(tmp_path: Path) -> None:
    repo, env = _clean_repo(tmp_path)
    payload = b"\x89PNG\r\n\x1a\nprivate"
    (repo / "private.png").write_bytes(payload)
    (repo / "public.dat").write_bytes(payload)
    subprocess.run(["git", "add", "private.png", "public.dat"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "leak"], cwd=repo, check=True, env=env)
    (repo / "private.png").unlink()
    subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "hide path"], cwd=repo, check=True, env=env)

    result = _audit(repo, env, "--history")

    assert result.returncode != 0
    assert "private.png: binary/media artifact" in result.stdout


def test_audit_rejects_disguised_binary_and_secret(tmp_path: Path) -> None:
    repo, env = _clean_repo(tmp_path)
    (repo / "payload.dat").write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    (repo / "notes.txt").write_text("Bearer " + "a" * 24, encoding="utf-8")
    (repo / ".env").write_text("PASS" + "WORD=hunter2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "payload.dat", "notes.txt", ".env"], cwd=repo, check=True)

    result = _audit(repo, env)

    assert result.returncode != 0
    assert "binary/media content" in result.stdout
    assert "secret or private-key pattern" in result.stdout
    assert "environment/credential file" in result.stdout


def test_audit_rejects_prefixed_credential_names(tmp_path: Path) -> None:
    repo, env = _clean_repo(tmp_path)
    (repo / "notes.txt").write_text(
        "ANTHROPIC_API_KEY=plainsecretvalue\n"
        "GITHUB_TOKEN=plainsecretvalue\n"
        "AWS_SECRET_ACCESS_KEY=plainsecretvalue\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)

    result = _audit(repo, env)

    assert result.returncode != 0
    assert "secret or private-key pattern" in result.stdout


def test_history_audit_rejects_sensitive_commit_message(tmp_path: Path) -> None:
    repo, env = _clean_repo(tmp_path)
    message = "private home /" + "Users/alice/work"
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", message], cwd=repo, check=True, env=env)

    result = _audit(repo, env, "--history")

    assert result.returncode != 0
    assert "commit message: absolute user-home path" in result.stdout


def _clean_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Keep Going",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Keep Going",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "KEEP_GOING_PRIVACY_ROOT": str(repo),
    }
    subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=repo, check=True, env=env)
    return repo, env


def _audit(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "privacy-audit.py"), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        env=env,
    )
