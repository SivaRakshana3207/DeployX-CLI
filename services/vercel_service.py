"""
Vercel helpers used by the DeployX CLI.

The Vercel *authentication* (OAuth device flow) lives in cli/auth.py.
This module performs the real Vercel REST work that ``deployx deploy``
needs:

* find or create the Vercel project, so a redeploy reuses the same one,
* create a REAL deployment from the pushed GitHub repository,
* poll the REAL deployment status until it reaches a terminal state,
* read the REAL build error when that deployment fails.

Nothing here invents a URL, an id or a status: every value returned comes
straight from an API response. When Vercel reports an error, DeployX
reports that error - it never pretends a deployment succeeded.

The Vercel API token is never logged or printed.
"""

from __future__ import annotations

import os
import time

import requests

VERCEL_USERINFO_URL = "https://api.vercel.com/login/oauth/userinfo"
VERCEL_USER_URL = "https://api.vercel.com/v2/user"
VERCEL_API_URL = "https://api.vercel.com"

# Upper bound on how long one CLI deployment waits for a real Vercel build.
# It is a ceiling, never a fake delay: polling stops the moment Vercel
# reports a terminal state.
DEPLOY_TIMEOUT_SECONDS = int(
    os.getenv("DEPLOYX_VERCEL_TIMEOUT", "600")
)

POLL_INTERVAL_SECONDS = 5

# Never uploaded to Vercel, whatever the project's own .gitignore says.
NEVER_UPLOAD = {
    ".git",
    ".github",
    ".deployx",
    ".vercel",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "venv",
    "secrets",
}

# Directory names never traversed while collecting files.
SKIP_DIRS = NEVER_UPLOAD | {"dist", "build"}

SECRET_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
)

MAX_UPLOAD_FILES = 1000


class VercelError(RuntimeError):
    """A real, token-free Vercel failure."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# Captured at import time so the ``except`` clauses below always reference the
# real exception classes, even when ``requests`` is replaced (as it is in
# unit tests).
_REQUESTS_TIMEOUT = requests.Timeout
_REQUESTS_ERROR = requests.RequestException


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "DeployX",
    }


def _error_detail(response) -> str:
    """Vercel's own reason for a failure, bounded in length."""
    try:
        payload = response.json() or {}
    except Exception:
        return ""

    error = payload.get("error") if isinstance(payload, dict) else None

    if isinstance(error, dict):
        return str(error.get("message") or "")[:300]

    if isinstance(error, str):
        return error[:300]

    message = (
        payload.get("message") if isinstance(payload, dict) else None
    )
    return str(message or "")[:300]


def _request(
    method: str,
    path: str,
    token: str,
    *,
    json_body=None,
    params=None,
    timeout: int = 30,
) -> dict:
    """
    Perform one Vercel API call with beginner-friendly error handling.

    Covers 401 / 403 / 404 / 429 / 5xx, network timeouts and unreadable
    responses. The token never appears in any message.
    """
    if not token:
        raise VercelError(
            "not_configured",
            "Vercel authentication is missing. "
            "Please check VERCEL_TOKEN.",
        )

    try:
        response = requests.request(
            method,
            f"{VERCEL_API_URL}{path}",
            headers=_headers(token),
            json=json_body,
            params=params,
            timeout=timeout,
        )
    except _REQUESTS_TIMEOUT:
        raise VercelError(
            "timeout",
            "Vercel did not respond in time. "
            "Please try again in a moment.",
        ) from None
    except _REQUESTS_ERROR:
        raise VercelError(
            "network_error",
            "Could not reach Vercel. Please check your internet "
            "connection.",
        ) from None

    if response.status_code == 401:
        raise VercelError(
            "unauthorized",
            "Vercel authentication failed. "
            "Please check VERCEL_TOKEN.",
            401,
        )

    if response.status_code == 403:
        raise VercelError(
            "forbidden",
            "This Vercel token is not allowed to perform that action.",
            403,
        )

    if response.status_code == 404:
        raise VercelError(
            "not_found",
            "Vercel could not find that project or deployment.",
            404,
        )

    if response.status_code == 429:
        raise VercelError(
            "rate_limited",
            "Vercel is receiving too many requests. "
            "Please wait a moment and try again.",
            429,
        )

    if response.status_code >= 500:
        raise VercelError(
            "server_error",
            "Vercel had a server problem. Please try again.",
            500,
        )

    if response.status_code >= 400:
        detail = _error_detail(response) or "no details provided"
        raise VercelError(
            "api_error",
            f"Vercel rejected the request: {detail}",
            response.status_code,
        )

    try:
        payload = response.json() or {}
    except Exception:
        raise VercelError(
            "invalid_response",
            "Vercel returned a response DeployX could not read.",
        ) from None

    return payload if isinstance(payload, dict) else {}


def get_authenticated_user(token: str) -> dict | None:
    """Return basic info for the token owner, or ``None`` if invalid."""
    if not token:
        return None

    # OIDC userinfo first (works with device-flow access tokens).
    try:
        response = requests.get(
            VERCEL_USERINFO_URL,
            headers=_headers(token),
            timeout=20,
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                return {
                    "username": (
                        data.get("preferred_username")
                        or data.get("email")
                        or data.get("name")
                    ),
                    "email": data.get("email"),
                    "name": data.get("name"),
                    "sub": data.get("sub"),
                }
    except requests.RequestException:
        pass

    # Fallback to the REST user endpoint.
    try:
        response = requests.get(
            VERCEL_USER_URL,
            headers=_headers(token),
            timeout=20,
        )
        if response.status_code == 200:
            data = response.json() or {}
            user = data.get("user", data)
            return {
                "username": user.get("username") or user.get("email"),
                "email": user.get("email"),
                "name": user.get("name"),
                "sub": user.get("id"),
            }
    except requests.RequestException:
        pass

    return None


def validate_token(token: str) -> bool:
    return get_authenticated_user(token) is not None


# =========================================================
# FRAMEWORK -> VERCEL PROJECT SETTINGS
# =========================================================

# Vercel auto-detects only a few frameworks. An empty mapping is safe and
# deliberate: Vercel then applies its own defaults, which is exactly what
# a plain React + Vite project needs.
FRAMEWORK_SETTINGS = {
    "nextjs": {"framework": "nextjs"},
    "static-html": {"framework": None},
    "flask": {"framework": None},
    "react-vite": {"framework": None},
    "vue-vite": {"framework": None},
}


def _project_settings(framework: str | None) -> dict:
    if not framework:
        return {}

    settings = FRAMEWORK_SETTINGS.get(framework)

    return dict(settings) if settings else {}


# =========================================================
# PROJECT: FIND OR CREATE (so a redeploy reuses it)
# =========================================================

def get_project(token: str, project_name: str) -> dict | None:
    """Return an existing Vercel project, or ``None`` when absent."""
    try:
        payload = _request(
            "GET",
            f"/v9/projects/{project_name}",
            token,
        )
    except VercelError as exc:
        if exc.code == "not_found":
            return None
        raise

    return payload or None


def ensure_project(
    token: str,
    project_name: str,
    framework: str | None = None,
) -> dict:
    """
    Return the Vercel project, creating it only when it does not exist.

    This is what stops a second ``deployx deploy`` from producing
    ``demo-project-2`` - the same project name is always reused.
    """
    existing = get_project(token, project_name)

    if existing:
        existing["created"] = False
        return existing

    body = {"name": project_name}
    body.update(_project_settings(framework))

    payload = _request(
        "POST",
        "/v10/projects",
        token,
        json_body=body,
    )

    payload["created"] = True
    return payload


# =========================================================
# FILE COLLECTION (the real deployment payload)
# =========================================================

def _is_secret_file(relative_path: str) -> bool:
    """``.env`` files and private keys never leave the machine."""
    name = relative_path.replace("\\", "/").split("/")[-1].lower()

    if name == ".env" or name.startswith(".env."):
        return True

    return name.endswith(SECRET_FILE_SUFFIXES)

def collect_project_files(project_path) -> list:
    """
    Collect the project source for a Vercel deployment.

    Secrets and build artefacts are excluded on purpose: ``.env``, private
    keys, ``node_modules``, ``.git`` and friends are never uploaded.
    """
    import base64

    root = str(project_path)
    files = []

    for current, directories, filenames in os.walk(root):
        directories[:] = [
            name for name in directories
            if name not in NEVER_UPLOAD
            and not name.endswith(".egg-info")
        ]

        for filename in filenames:
            full = os.path.join(current, filename)
            relative = os.path.relpath(full, root).replace("\\", "/")

            if _is_secret_file(relative):
                continue

            try:
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue

            files.append(
                {
                    "file": relative,
                    "data": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                }
            )

            if len(files) >= MAX_UPLOAD_FILES:
                return files

    return files


# =========================================================
# DEPLOYMENT
# =========================================================

def create_deployment(
    token: str,
    project_name: str,
    project_path,
    framework: str | None = None,
) -> dict:
    """
    Upload the project and trigger a REAL Vercel deployment.

    Returns Vercel's own response: the deployment id, its url and the
    initial ``readyState``. No URL is ever generated locally.
    """
    files = collect_project_files(project_path)

    if not files:
        raise VercelError(
            "empty_project",
            "No project files were found to deploy.",
        )

    return _request(
        "POST",
        "/v13/deployments",
        token,
        json_body={
            "name": project_name,
            "files": files,
            "projectSettings": _project_settings(framework),
            "target": "production",
        },
        timeout=120,
    )


def get_deployment(token: str, deployment_id: str) -> dict:
    """Read the current, real state of one deployment."""
    if not deployment_id:
        raise VercelError(
            "invalid_request",
            "A deployment id is required.",
        )

    return _request(
        "GET",
        f"/v13/deployments/{deployment_id}",
        token,
    )


def _public_url(payload: dict) -> str:
    """
    Prefer the stable production alias over the per-build URL.

    Vercel returns an ``alias`` list; the production alias is the address a
    developer can actually share.
    """
    for alias in payload.get("alias") or []:
        name = str(alias)

        if not name or "-git-" in name:
            continue

        return name if name.startswith("http") else f"https://{name}"

    url = payload.get("url") or ""

    if not url:
        return ""

    return url if str(url).startswith("http") else f"https://{url}"


def _extract_error(payload: dict) -> str:
    """Vercel's own failure message, never build secrets."""
    error = payload.get("error") or {}
    message = error.get("message") if isinstance(error, dict) else error

    if message:
        return str(message)[:500]

    if (payload.get("readyState") or "").upper() == "ERROR":
        return "Vercel reported a build error without further detail."

    return ""


# =========================================================
# MONITORING
# =========================================================

# The deployment states DeployX shows in the terminal.
STATUS_PENDING = "PENDING"
STATUS_BUILDING = "BUILDING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

FINAL_STATES = {STATUS_SUCCESS, STATUS_FAILED}


def map_ready_state(ready_state: str) -> str:
    """
    Translate a real Vercel ``readyState`` into a DeployX status.

    Vercel is the single source of truth; this is a display mapping only.
    """
    state = (ready_state or "").upper()

    if state == "READY":
        return STATUS_SUCCESS

    if state in {"ERROR", "CANCELED", "CANCELLED"}:
        return STATUS_FAILED

    if state in {"BUILDING", "DEPLOYING"}:
        return STATUS_BUILDING

    return STATUS_PENDING


def is_final_state(status: str) -> bool:
    """True once DeployX should stop waiting."""
    return status in FINAL_STATES


def poll_deployment(
    token: str,
    deployment_id: str,
    on_update=None,
    timeout: int | None = None,
    interval: int | None = None,
) -> dict:
    """
    Poll the REAL Vercel API until the deployment reaches a final state.

    Stops on READY (success) or ERROR / CANCELED (failure), and stops on a
    timeout so the CLI never waits forever. Nothing is simulated: every
    status passed to ``on_update`` came from Vercel.
    """
    timeout = timeout or DEPLOY_TIMEOUT_SECONDS
    interval = interval or POLL_INTERVAL_SECONDS
    deadline = time.monotonic() + timeout

    while True:
        payload = get_deployment(token, deployment_id)
        ready_state = (payload.get("readyState") or "").upper()
        status = map_ready_state(ready_state)

        if on_update:
            on_update(status, ready_state, payload)

        if status == STATUS_SUCCESS:
            return {
                "status": STATUS_SUCCESS,
                "ready_state": ready_state,
                "deployment_id": deployment_id,
                "url": _public_url(payload),
                "error": "",
            }

        if status == STATUS_FAILED:
            return {
                "status": STATUS_FAILED,
                "ready_state": ready_state,
                "deployment_id": deployment_id,
                "url": "",
                "error": _extract_error(payload),
            }

        if time.monotonic() >= deadline:
            raise VercelError(
                "timeout",
                "The Vercel build is taking longer than expected. "
                "Check your Vercel dashboard for build progress.",
            )

        time.sleep(interval)

    if message:
        return str(message)[:500]

    if (payload.get("readyState") or "").upper() == "ERROR":
        return "Vercel reported a build error without further detail."

    return ""