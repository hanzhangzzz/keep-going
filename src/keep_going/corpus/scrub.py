"""Lightweight PII scrubber: paths, emails, token-shaped strings."""

from __future__ import annotations

import os
import re

_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Long alphanumeric tokens (>=24 chars) often look like API keys / tokens
_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_\-]{24,})\b")
# Common token prefixes to mask aggressively
_PREFIXED_TOKEN_RE = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]+|ghp_[A-Za-z0-9_\-]+|gho_[A-Za-z0-9_\-]+|xoxb-[A-Za-z0-9_\-]+|t-[A-Za-z0-9]{10,})\b"
)


def _real_username() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"


def _extra_hosts() -> list[str]:
    """Comma-separated hosts from KEEP_GOING_SCRUB_EXTRA_HOSTS (e.g. internal corp domains).

    Kept out of committed config on purpose: naming the domains in-repo would
    itself leak them. Subdomains of each listed host are masked too."""
    raw = os.environ.get("KEEP_GOING_SCRUB_EXTRA_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def scrub(text: str, *, user_replacement: str = "USER", real_user: str | None = None) -> str:
    if not text:
        return text
    real = real_user if real_user is not None else _real_username()
    out = text.replace(f"/Users/{real}", f"/Users/{user_replacement}")
    out = out.replace(f"/home/{real}", f"/home/{user_replacement}")
    out = _PREFIXED_TOKEN_RE.sub("<REDACTED_TOKEN>", out)
    out = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@***", out)
    for host in _extra_hosts():
        pattern = re.compile(r"\b[A-Za-z0-9.-]*" + re.escape(host) + r"\b", re.IGNORECASE)
        out = pattern.sub("<REDACTED_HOST>", out)
    # Mask long alphanumeric runs only if they look high-entropy
    out = _TOKEN_RE.sub(lambda m: _maybe_mask(m.group(1)), out)
    return out


def _maybe_mask(s: str) -> str:
    # Heuristic: high digit+letter mix and >=24 chars → mask
    if len(s) < 24:
        return s
    has_digit = any(c.isdigit() for c in s)
    has_alpha = any(c.isalpha() for c in s)
    if has_digit and has_alpha:
        return f"<REDACTED_{len(s)}>"
    return s
