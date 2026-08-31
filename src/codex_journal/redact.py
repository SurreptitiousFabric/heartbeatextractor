from __future__ import annotations

import re
from pathlib import Path


PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL | re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
PREFIX_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*)\s*=\s*([^\s,;]+)"
)
PASSWORD_RE = re.compile(r"(?i)\b(password|passwd)\s*[:=]\s*([^\s,;]+)")
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
ENV_LINE_RE = re.compile(r"(?m)^\s*[A-Z_][A-Z0-9_]*=.*$")


def redact_text(text: str, home: Path | None = None) -> tuple[str | None, int]:
    """Return safe text and replacement count; omit environment dumps."""

    if len(ENV_LINE_RE.findall(text)) >= 3:
        return None, 1

    count = 0

    def replace(pattern: re.Pattern[str], value: str, replacement: str) -> str:
        nonlocal count
        value, replacements = pattern.subn(replacement, value)
        count += replacements
        return value

    safe = text
    safe = replace(PRIVATE_KEY_RE, safe, "[REDACTED PRIVATE KEY]")
    safe = replace(BEARER_RE, safe, "Bearer [REDACTED]")

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}=[REDACTED]"

    safe = SECRET_ASSIGNMENT_RE.sub(assignment, safe)
    safe = replace(PREFIX_TOKEN_RE, safe, "[REDACTED CREDENTIAL]")

    def password(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}: [REDACTED]"

    safe = PASSWORD_RE.sub(password, safe)

    def url_credential(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]@"

    safe = URL_CREDENTIAL_RE.sub(url_credential, safe)

    home_path = str(home or Path.home())
    if home_path and home_path != "/":
        safe = safe.replace(home_path, "~")
    return safe, count


def shorten_home(value: str | None, home: Path | None = None) -> str | None:
    if value is None:
        return None
    home_path = str(home or Path.home())
    if value == home_path:
        return "~"
    prefix = home_path.rstrip("/") + "/"
    if value.startswith(prefix):
        return "~/" + value[len(prefix) :]
    return value
