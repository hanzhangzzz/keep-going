"""Shared privacy checks for Git trees and distributable packages."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


PRIVATE_ARTIFACT_RE = re.compile(
    r"^artifacts/(?:\.)?decision-(?!policy\.template\.yaml$)[^/]+\.ya?ml(?:\.(?:bak|tmp))?$"
)
EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
HOME_PATH_RE = re.compile(
    rb"(?:/" rb"Users/(?!(?:USER|sample)(?:/|\b)|<[^>]+>|\{[^}]+\})[^/\s]+|"
    rb"[A-Za-z]:\\" rb"Users\\[^\\\s]+)"
)
MEDIA_SUFFIXES = {
    ".7z", ".avi", ".doc", ".docx", ".gif", ".gz", ".heic", ".jpeg",
    ".jpg", ".mov", ".mp3", ".mp4", ".pdf", ".png", ".ppt", ".pptx",
    ".rar", ".svg", ".tar", ".tiff", ".wav", ".webp", ".xls", ".xlsx", ".zip",
}
FORBIDDEN_NAMES = {"claude.log", "save.txt"}
FORBIDDEN_PREFIXES = (".omx/", ".serena/", "data/")
FORBIDDEN_PATHS = {".claude/settings.local.json"}
SAFE_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "users.noreply.github.com"}
# Coding agents append a Co-Authored-By trailer carrying this public no-reply
# address. It identifies no person, so allow the exact address rather than the
# whole anthropic.com domain, which would also let real staff mail through.
SAFE_EMAIL_ADDRESSES = {"noreply@anthropic.com"}
SECRET_PATTERNS = (
    re.compile(rb"-" * 5 + rb"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY" + rb"-" * 5),
    re.compile(
        rb"(?:s" rb"k-(?:proj-)?[A-Za-z0-9_-]{16,}|gh" rb"[po]_[A-Za-z0-9_-]{16,}|"
        rb"github_pat_[A-Za-z0-9_]{16,}|xox" rb"[baprs]-[A-Za-z0-9_-]{16,})"
    ),
    re.compile(rb"(?i:Bearer)[ \t]+[A-Za-z0-9._~-]{20,}"),
    re.compile(
        rb"(?m)^[ \t]*(?:export[ \t]+)?(?=[A-Z0-9_]*(?:PASS" rb"WORD|PASSWD|API_KEY|SECRET|TOKEN|"
        rb"ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*[ \t]*=)[A-Z][A-Z0-9_]*[ \t]*=[ \t]*[\"']?[^\s\"'#]{6,}"
    ),
)
MEDIA_MAGIC = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"%PDF-",
    b"PK\x03\x04", b"\x1f\x8b", b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"II*\x00", b"MM\x00*",
)
REVIEWED_MEDIA_SHA256 = {
    "docs/assets/keep-going-concept.svg": "2e76241a2e5221f3b7548b7d7e4bcb01d94efa6866a485c12fe409aeae40caa7",
}
REVIEWED_MEDIA_HISTORICAL_SHA256 = {
    "docs/assets/keep-going-concept.svg": {
        "a749b6a1f654b0842dd686846ffe3e18d3b8e0c290910c1dd98744c8e1666188",
        "d025afc456af3ae58e48d06b9f783544312c089ab4f9f752781609d3bdc7f5d8",
    },
}
UNSAFE_SVG_RE = re.compile(
    rb"(?i)<\s*script\b|<\s*foreignObject\b|\bon[a-z]+\s*=|\bhref\s*=|\burl\s*\("
)


def path_violations(path: str) -> list[str]:
    normalized = path.replace("\\", "/")
    violations: list[str] = []
    if PRIVATE_ARTIFACT_RE.fullmatch(normalized):
        violations.append("private decision policy artifact")
    if normalized in FORBIDDEN_PATHS or normalized.startswith(FORBIDDEN_PREFIXES):
        violations.append("local/session path")
    if any(part == ".env" or part.startswith(".env.") for part in normalized.split("/")):
        violations.append("environment/credential file")
    if (
        Path(normalized).name in FORBIDDEN_NAMES
        or Path(normalized).suffix.lower() == ".debug"
        or normalized.endswith(".jsonl")
    ):
        violations.append("session/log artifact")
    if Path(normalized).suffix.lower() in MEDIA_SUFFIXES and normalized not in REVIEWED_MEDIA_SHA256:
        violations.append("binary/media artifact")
    return violations


def reviewed_media_violations(path: str, data: bytes) -> list[str]:
    normalized = path.replace("\\", "/")
    expected = REVIEWED_MEDIA_SHA256.get(normalized)
    if expected is None:
        return []
    violations = []
    actual = hashlib.sha256(data).hexdigest()
    allowed = {expected, *REVIEWED_MEDIA_HISTORICAL_SHA256.get(normalized, set())}
    if actual not in allowed:
        violations.append("reviewed media hash mismatch")
    if normalized.endswith(".svg") and UNSAFE_SVG_RE.search(data):
        violations.append("unsafe SVG content")
    return violations


def content_violations(data: bytes) -> list[str]:
    violations: list[str] = []
    if b"\0" in data:
        violations.append("binary content")
    if data.startswith(MEDIA_MAGIC) or (len(data) >= 12 and data[4:8] == b"ftyp"):
        violations.append("binary/media content")
    if HOME_PATH_RE.search(data):
        violations.append("absolute user-home path")
    domains = {
        match.group(1).decode("ascii", errors="ignore").lower()
        for match in EMAIL_RE.finditer(data)
        if match.group(0).decode("ascii", errors="ignore").lower() not in SAFE_EMAIL_ADDRESSES
    }
    if domains - SAFE_EMAIL_DOMAINS:
        violations.append("non-placeholder email address")
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):
        violations.append("secret or private-key pattern")
    return violations
