"""
Groq AI error analysis for DeployX.

Groq is NOT responsible for deploying anything. Vercel is the single source
of truth for deployment status; Groq is only used *after* Vercel reports a
real failure, to explain it in beginner-friendly language.

Safety rules enforced here:

* the API key is never printed, logged or returned,
* ``.env`` contents are never read and never sent,
* secret-looking values are redacted from anything before it is sent,
* a Groq outage never breaks a deployment - it degrades to a short note.
"""

from __future__ import annotations

import json
import os
import re

import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_MODEL = "openai/gpt-oss-20b"

DEFAULT_TIMEOUT = 60

MAX_LOGS_CHARS = 4000

# Anything shaped like a secret is masked before leaving the machine.
SECRET_VALUE_PATTERN = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{10,}"
    r"|vercel[ca]_[A-Za-z0-9_-]{10,}"
    r"|(?:sk|vcx)_[A-Za-z0-9_-]{10,}"
    r"|\b[0-9a-f]{40}\b)",
    re.IGNORECASE,
)

ENV_ASSIGNMENT_PATTERN = re.compile(
    r"^([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*=\s*\S+",
    re.IGNORECASE | re.MULTILINE,
)

REDACTED = "***redacted***"

UNAVAILABLE = (
    "Groq AI analysis unavailable. "
    "Deployment result will still be shown."
)


class GroqError(RuntimeError):
    """A safe, token-free Groq failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# Captured at import time so the ``except`` clauses below always reference the
# real exception classes, even when ``requests`` is replaced in unit tests.
_REQUESTS_TIMEOUT = requests.Timeout
_REQUESTS_ERROR = requests.RequestException


def _api_key() -> str:
    return (os.getenv("GROQ_API_KEY") or "").strip()


def _model() -> str:
    return (os.getenv("GROQ_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def is_configured() -> bool:
    """True when Groq analysis is available."""
    return bool(_api_key())


def redact(text: str) -> str:
    """
    Mask secret-looking values so nothing sensitive is ever sent to Groq.
    """
    if not text:
        return ""

    cleaned = ENV_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={REDACTED}", str(text)
    )

    return SECRET_VALUE_PATTERN.sub(REDACTED, cleaned)


def _truncate(text: str, limit: int = MAX_LOGS_CHARS) -> str:
    text = str(text or "")

    if len(text) <= limit:
        return text

    # Keep the tail: build errors show the failing line at the end.
    return "..." + text[-limit:]


def build_prompt(
    project: str,
    framework: str,
    error: str,
    logs: str = "",
    changed_files=None,
) -> str:
    """
    Build the analysis request sent to Groq.

    Redaction happens here as well as at the call site, so a secret can
    never be sent even if a future caller forgets to redact.
    """
    files = ", ".join(changed_files or []) or "not available"

    return (
        "You are DeployX, a beginner-friendly deployment assistant.\n\n"
        "Analyze this real deployment failure.\n\n"
        f"Project:\n{redact(project)}\n\n"
        f"Framework:\n{redact(framework)}\n\n"
        f"Deployment error:\n{redact(error)}\n\n"
        f"Relevant logs:\n{_truncate(redact(logs))}\n\n"
        f"Changed files if available:\n{files}\n\n"
        "Explain:\n"
        "1. What went wrong?\n"
        "2. Why did it happen?\n"
        "3. Which file is likely responsible?\n"
        "4. What exactly should the developer change?\n"
        "5. What command or action should they perform next?\n\n"
        "Do not invent information. "
        "If the evidence is insufficient, clearly say what is unknown.\n\n"
        "Reply with JSON only, using exactly these keys:\n"
        '{"summary": "...", "cause": "...", "affected_files": [], '
        '"recommended_fix": "...", "commands": [], '
        '"confidence": "high|medium|low"}'
    )
def _fallback(content: str) -> dict:
    """An honest low-confidence result when JSON parsing fails."""
    return {
        "summary": _truncate(content, 500),
        "cause": "Groq did not return a structured explanation.",
        "affected_files": [],
        "recommended_fix": "Review the build error above.",
        "commands": [],
        "confidence": "low",
    }


def _parse_response(content: str) -> dict:
    """
    Turn Groq's reply into the documented structure.

    Tolerates a JSON code fence and falls back to an honest low-confidence
    result when the model answers with prose instead of JSON.
    """
    text = str(content or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()

    data = None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")

        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        return _fallback(text)

    affected = data.get("affected_files")
    commands = data.get("commands")

    confidence = str(data.get("confidence") or "low").strip().lower()

    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    return {
        "summary": str(data.get("summary") or ""),
        "cause": str(data.get("cause") or ""),
        "affected_files": (
            [str(item) for item in affected] if isinstance(affected, list)
            else []
        ),
        "recommended_fix": str(data.get("recommended_fix") or ""),
        "commands": (
            [str(item) for item in commands] if isinstance(commands, list)
            else []
        ),
        "confidence": confidence,
    }


def analyze_build_error(
    project: str,
    framework: str,
    error: str,
    logs: str = "",
    changed_files=None,
) -> dict | None:
    """
    Ask Groq to explain a REAL deployment failure.

    Returns the documented structure, or ``None`` when Groq is not
    configured. Raises :class:`GroqError` only with a safe, token-free
    message so the caller can show it without hiding the real error.
    """
    if not is_configured():
        return None

    prompt = build_prompt(
        project=project,
        framework=framework,
        error=redact(error),
        logs=_truncate(redact(logs)),
        changed_files=changed_files,
    )

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={
                # The key authenticates the request and is never echoed.
                "Authorization": f"Bearer {_api_key()}",
                "Content-Type": "application/json",
                "User-Agent": "DeployX",
            },
            json={
                "model": _model(),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You explain deployment build failures to "
                            "beginners. You reply with JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 1200,
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except _REQUESTS_TIMEOUT:
        raise GroqError("timeout", UNAVAILABLE) from None
    except _REQUESTS_ERROR:
        raise GroqError("network_error", UNAVAILABLE) from None

    if response.status_code == 401:
        raise GroqError(
            "unauthorized",
            "Groq AI analysis unavailable. "
            "Please check GROQ_API_KEY.",
        )

    if response.status_code == 429:
        raise GroqError("rate_limited", UNAVAILABLE)

    if response.status_code >= 400:
        raise GroqError("api_error", UNAVAILABLE)

    try:
        payload = response.json() or {}
    except Exception:
        raise GroqError("invalid_response", UNAVAILABLE) from None

    choices = payload.get("choices") or []

    if not isinstance(choices, list) or not choices:
        raise GroqError("empty_response", UNAVAILABLE)

    message = choices[0].get("message") or {}

    return _parse_response(message.get("content") or "")
