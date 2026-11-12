"""Evidence is untrusted data. Redaction is configurable, not a DLP guarantee."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|cookie|api[_-]?key", re.I)


def redact(value, patterns: tuple[str, ...] = ()):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SECRET_KEY.search(str(k)) else redact(v, patterns)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, patterns) for v in value]
    if not isinstance(value, str):
        return value
    # Recursively redact embedded JSON response/request bodies.
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (dict, list)):
            return json.dumps(redact(parsed, patterns), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    value = re.sub(r"(?im)((?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    value = re.sub(r'''(?ix)(["']?(?:password|passwd|secret|access_token|refresh_token|token|api[_-]?key)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s&,;<>]+)''', r"\1[REDACTED]", value)
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", value)
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED]", value)
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_relative(name: str) -> str:
    if not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Invalid artifact path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ("..", ".") for p in name.split("/")):
        raise ValueError("Artifact paths must stay under the report directory")
    return str(path)


def normalized_message(message: str) -> str:
    message = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", message, flags=re.I)
    message = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.+Z-]+", "<time>", message)
    return re.sub(r"\s+", " ", message).strip()


def fingerprint(project: str, failure: dict) -> str:
    return digest([project, failure.get("exception"), failure.get("source"),
                   failure.get("endpoint"), failure.get("locator"),
                   normalized_message(failure.get("message", ""))])
