from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from cli.analyzer import analyze
from cli.builder import run_build
from cli.api_client import create_deployment
from services.deployment_service import update_deployment
from cli.auth import (
    github_login,
    vercel_login,
    get_provider_credentials,
    is_authenticated,
    logout,
    print_auth_status,
    ensure_authenticated,
)

# Repository creation runs with the authenticated user's own token so
# the repo always lives in that user's GitHub account.
from services.github_service import (
    ensure_repository as ensure_github_repository,
)

# Real Vercel REST work: find-or-create the project, trigger a real
# deployment and poll the real status. Vercel - not n8n and not the AI -
# is the single source of truth for whether a deployment succeeded.
from services.vercel_service import (
    VercelError,
    create_deployment as create_vercel_deployment,
    ensure_project as ensure_vercel_project,
    map_ready_state,
    poll_deployment,
)

# Groq is only used AFTER Vercel reports a real failure.
from services.groq_service import (
    GroqError,
    analyze_build_error as groq_analyze_build_error,
    is_configured as groq_is_configured,
)

# The DeployX backend (token broker) and n8n are OPTIONAL. When they are
# unavailable the CLI continues with the direct GitHub -> Vercel pipeline.
from cli import backend_client
from cli.backend_client import (
    backend_url,
    prepare_deployment_ticket,
)

# Built-in backend configuration (see config.py). These n8n webhook
# URLs ship with DeployX, so users never set n8n environment
# variables themselves.
from config import (
    DEPLOYX_N8N_WEBHOOK_URL,
    DEPLOYX_N8N_VERCEL_WEBHOOK_URL,
)


SUPPORTED_FRAMEWORKS = {
    "static-html",
    "flask",
    "react-vite",
    "nextjs",
    "vue-vite",
}


# =========================================================
# UI
# =========================================================

def line():
    print("─" * 64)


def print_header():
    print()
    print("╭" + "─" * 62 + "╮")
    print("│" + " " * 20 + "🚀 DeployX" + " " * 31 + "│")
    print("│" + " " * 19 + "Phase 2 CLI" + " " * 31 + "│")
    print("╰" + "─" * 62 + "╯")
    print()


# =========================================================
# CONSOLE ENCODING (Windows-safe Unicode output)
# =========================================================

def configure_console_encoding():
    """
    Make DeployX console output safe on every terminal.

    The CLI prints Unicode (box drawing, check marks, emoji).
    When stdout is redirected, or when the Windows console uses
    a legacy code page, those characters raise
    UnicodeEncodeError. Switching the standard streams to UTF-8
    keeps the exact same output working everywhere.
    """

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            pass


# =========================================================
# REPOSITORY NAME SANITIZATION
# =========================================================

def sanitize_repo_name(name):
    """
    Turn a folder name into a safe GitHub/Vercel project name.

    GitHub allows alphanumeric, '-', '_' and '.'. Anything else
    becomes '-'. Empty results fall back to 'deployx-app'.
    """
    import re

    cleaned = (name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-").strip(".")
    if not cleaned:
        return "deployx-app"
    return cleaned[:100]


# =========================================================
# GITHUB AUTHENTICATION
# =========================================================

def ensure_github_authentication():
    """
    Make sure DeployX has a valid GitHub login.

    If the user is not authenticated, GitHub Device Flow
    authentication is started automatically.
    """

    if is_authenticated("github"):
        credentials = get_provider_credentials("github")

        username = credentials.get(
            "username",
            "GitHub user",
        )

        print(
            f"✓ GitHub authentication: connected "
            f"({username})"
        )

        return credentials

    print()
    print(
        "🔐 GitHub authentication required."
    )

    print(
        "DeployX will open GitHub authorization "
        "in your browser."
    )

    print()

    try:

        credentials = github_login()

        if not credentials:
            raise RuntimeError(
                "GitHub authentication did not return credentials."
            )

        username = credentials.get(
            "username",
            "GitHub user",
        )

        print()
        print(
            f"✓ GitHub authentication successful: "
            f"{username}"
        )

        return credentials

    except Exception as exc:

        raise RuntimeError(
            f"GitHub authentication failed: {exc}"
        ) from exc


# =========================================================
# GIT CREDENTIAL HELPER
# =========================================================

def _create_git_askpass_script(
    temp_directory,
):
    """
    Create a temporary Git ASKPASS script.

    The actual GitHub token is NOT written into the script.
    It is supplied through an environment variable only.
    """

    script_path = (
        Path(temp_directory)
        / "deployx_git_askpass.cmd"
    )

    script_content = r"""@echo off
set "PROMPT=%~1"

echo %PROMPT% | findstr /I "username" >nul
if %ERRORLEVEL% EQU 0 (
    echo %DEPLOYX_GIT_USERNAME%
) else (
    echo %DEPLOYX_GIT_TOKEN%
)
"""

    script_path.write_text(
        script_content,
        encoding="utf-8",
    )

    return script_path


def _git_authenticated_environment(
    credentials,
    temp_directory,
):
    """
    Prepare environment variables for Git authentication.

    The GitHub token stays outside the remote URL.
    """

    token = credentials.get(
        "access_token"
    )

    username = credentials.get(
        "username",
        "DeployX",
    )

    if not token:
        raise RuntimeError(
            "GitHub access token is missing "
            "from DeployX authentication."
        )

    askpass_script = (
        _create_git_askpass_script(
            temp_directory
        )
    )

    environment = {
        **os.environ,

        "GIT_TERMINAL_PROMPT": "0",

        "GIT_ASKPASS": str(
            askpass_script
        ),

        "GIT_USERNAME": username,

        "DEPLOYX_GIT_USERNAME": username,

        "DEPLOYX_GIT_TOKEN": token,
    }

    return environment


# =========================================================
# REPORT AN n8n FAILURE HONESTLY
# =========================================================

# Human-readable titles for the workflow's failure_stage values.
_FAILURE_STAGES = {
    "authorization": "Deployment ticket or provider authorization rejected",
    "backend_error": "DeployX backend returned an error",
    "backend_unreachable": "DeployX backend is unreachable from n8n",
    "identity_mismatch": "Repository does not belong to the authenticated user",
    "push_not_confirmed": "Git push was not confirmed",
    "vercel_api": "Vercel API error",
    "vercel_build": "Vercel build failed",
    "vercel_timeout": "Timed out waiting for the Vercel build",
}


def _report_n8n_failure(result: dict):
    """
    Print the real reason a deployment failed.

    The workflow's ``error`` field carries the actual backend/provider cause and
    is always shown first. ``ai_fix_guidance`` is advisory text and is printed
    afterwards, clearly labelled, so a generic suggestion is never presented as
    the diagnosis.
    """

    if not isinstance(result, dict):
        print()
        print("✗ Vercel deployment failed.")
        print(f"  {result}")
        return

    stage = str(result.get("failure_stage") or "").strip()
    error = str(result.get("error") or "").strip()
    guidance = str(result.get("ai_fix_guidance") or "").strip()

    print()
    print("✗ Vercel deployment failed.")

    if stage:
        title = _FAILURE_STAGES.get(
            stage, f"Deployment failed ({stage})"
        )
        print(f"  Stage: {title}")

    # The backend's own message is the authoritative reason.
    print(f"  Reason: {error or 'n8n did not report a reason.'}")

    if result.get("vercel_deployment_id"):
        print(
            f"  Vercel deployment ID: "
            f"{result['vercel_deployment_id']}"
        )

    if result.get("github_repository"):
        print(
            f"  Repository: "
            f"{result['github_repository']}"
        )

    if result.get("auth_required"):
        print()
        print(
            "  The backend reported an authorization problem. "
            "Run `deployx auth-status`"
        )
        print(
            "  and re-run `deployx login` if the authorization is "
            "missing or expired."
        )

    if guidance:
        print()
        print("  Suggested steps (AI-generated, may not match "
              "the actual cause):")
        for line in guidance.splitlines():
            if line.strip():
                print(f"    {line.strip()}")

    if not stage and not error:
        print()
        print(f"  Raw response: {json.dumps(result)[:500]}")


# =========================================================
# CALL n8n — GITHUB REPOSITORY
# =========================================================

def call_n8n(
    project_info,
    deployment_id,
    context=None,
    retries=3,
    timeout=60,
):
    """
    Send deployment information to the published n8n webhook.

    n8n creates the GitHub repository or reuses an existing one.
    """

    # Built into DeployX (see config.py). Users never set this.
    webhook_url = DEPLOYX_N8N_WEBHOOK_URL

    if not webhook_url:
        raise RuntimeError(
            "DEPLOYX_N8N_WEBHOOK_URL is not configured."
        )

    if "/webhook-test/" in webhook_url:
        raise RuntimeError(
            "DEPLOYX_N8N_WEBHOOK_URL is using the test webhook. "
            "Use the production URL ending with /webhook/deployx."
        )

    payload = {
        "project_name": project_info["project_name"],
        "framework": project_info["framework"],
        "project_type": project_info["project_type"],
        "build_command": project_info.get(
            "build_command"
        ),
        "output_directory": project_info.get(
            "output_directory",
            ".",
        ),
        "deployment_id": deployment_id,
        "branch": "main",
        "env_var_names": project_info.get(
            "env_variable_names",
            [],
        ),
    }

    # User-specific context so the backend operates on the correct
    # user's deployment. No access tokens are included here.
    if context:
        for key in (
            "github_username",
            "github_repository",
            "github_url",
            "repository_name",
            "clone_url",
            "deployment_ticket",
        ):
            if context.get(key) is not None:
                payload[key] = context[key]

    data = json.dumps(
        payload
    ).encode("utf-8")

    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):

        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:

            print(
                f"   n8n request attempt "
                f"{attempt}/{retries}..."
            )

            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:

                response_data = (
                    response
                    .read()
                    .decode("utf-8")
                )

                if not response_data.strip():
                    raise RuntimeError(
                        "n8n returned an empty response."
                    )

                try:

                    return json.loads(
                        response_data
                    )

                except json.JSONDecodeError as exc:

                    raise RuntimeError(
                        "n8n returned invalid JSON: "
                        f"{response_data}"
                    ) from exc

        except urllib.error.HTTPError as exc:

            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            if (
                400 <= exc.code < 500
                and exc.code != 429
            ):

                raise RuntimeError(
                    f"n8n returned HTTP "
                    f"{exc.code}: {body}"
                ) from exc

            last_error = RuntimeError(
                f"n8n returned HTTP "
                f"{exc.code}: {body}"
            )

            print(
                f"   ⚠️ Temporary n8n error: "
                f"{last_error}"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:

            last_error = RuntimeError(
                f"Could not reach n8n: {exc}"
            )

            print(
                f"   ⚠️ n8n request failed: "
                f"{exc}"
            )

        except Exception as exc:

            last_error = exc

            print(
                f"   ⚠️ n8n request error: "
                f"{exc}"
            )

        if attempt < retries:

            wait_seconds = 2 * attempt

            print(
                f"   Retrying in "
                f"{wait_seconds} seconds..."
            )

            time.sleep(
                wait_seconds
            )

    raise RuntimeError(
        f"n8n request failed after "
        f"{retries} attempts: {last_error}"
    )


# =========================================================
# CALL n8n — VERCEL DEPLOYMENT
# =========================================================

def call_n8n_vercel(
    project_info,
    deployment_id,
    repository_url,
    commit_sha,
    context=None,
    retries=2,
    timeout=150,
):
    """
    Tell n8n that the GitHub push has been verified.

    n8n then triggers the Vercel deployment and waits
    for the deployment result.
    """

    # Built into DeployX (see config.py). Users never set this.
    webhook_url = DEPLOYX_N8N_VERCEL_WEBHOOK_URL

    if not webhook_url:

        raise RuntimeError(
            "DEPLOYX_N8N_VERCEL_WEBHOOK_URL "
            "is not configured."
        )

    if "/webhook-test/" in webhook_url:

        raise RuntimeError(
            "DEPLOYX_N8N_VERCEL_WEBHOOK_URL "
            "is using the test webhook. "
            "Use the production URL ending with "
            "/webhook/deployx-vercel."
        )

    commit_sha = commit_sha.strip()

    # This is only reached after push_to_github() succeeded AND
    # verify_github_push() matched the local HEAD against the remote main
    # branch, so the flag below reflects a real, confirmed push.
    payload = {
        "deployment_id": (
            f"DEP-{deployment_id:05d}"
        ),
        "project_name": (
            project_info["project_name"]
        ),
        "framework": (
            project_info["framework"]
        ),
        "project_type": (
            project_info.get("project_type")
        ),
        "branch": "main",
        # Canonical field name across CLI, n8n and the backend.
        "git_push_confirmed": True,
        "commit_sha": commit_sha,
        "env_var_names": project_info.get(
            "env_variable_names",
            [],
        ),
    }

    # Provider tokens are NEVER sent to n8n. When the DeployX backend is
    # available we forward a short-lived deployment_ticket instead, which the
    # backend exchanges for the user's own stored credentials.
    if context:
        for key in (
            "github_username",
            "github_repository",
            "vercel_username",
            "deployment_ticket",
        ):
            if context.get(key) is not None:
                payload[key] = context[key]

    # github_url must agree with github_repository. The repository always
    # belongs to the authenticated user, so derive the URL from that single
    # source of truth and only fall back to the passed-in URL.
    full_name = payload.get("github_repository")

    if full_name and "/" in full_name:
        payload["github_url"] = f"https://github.com/{full_name}"
    else:
        payload["github_url"] = repository_url

    data = json.dumps(
        payload
    ).encode("utf-8")

    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):

        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:

            print(
                f"   Vercel n8n request attempt "
                f"{attempt}/{retries}..."
            )

            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:

                response_data = (
                    response
                    .read()
                    .decode("utf-8")
                )

                if not response_data.strip():

                    raise RuntimeError(
                        "n8n Vercel workflow "
                        "returned an empty response."
                    )

                try:

                    return json.loads(
                        response_data
                    )

                except json.JSONDecodeError as exc:

                    raise RuntimeError(
                        "n8n Vercel workflow "
                        "returned invalid JSON: "
                        f"{response_data}"
                    ) from exc

        except urllib.error.HTTPError as exc:

            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            if (
                400 <= exc.code < 500
                and exc.code != 429
            ):

                raise RuntimeError(
                    f"n8n Vercel returned HTTP "
                    f"{exc.code}: {body}"
                ) from exc

            last_error = RuntimeError(
                f"n8n Vercel returned HTTP "
                f"{exc.code}: {body}"
            )

            print(
                f"   ⚠️ Temporary Vercel "
                f"workflow error: {last_error}"
            )

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:

            last_error = RuntimeError(
                f"Could not reach Vercel workflow: "
                f"{exc}"
            )

            print(
                f"   ⚠️ Vercel workflow "
                f"request failed: {exc}"
            )

        except Exception as exc:

            last_error = exc

            print(
                f"   ⚠️ Vercel workflow error: "
                f"{exc}"
            )

        if attempt < retries:

            wait_seconds = 3 * attempt

            print(
                f"   Retrying in "
                f"{wait_seconds} seconds..."
            )

            time.sleep(
                wait_seconds
            )

    raise RuntimeError(
        "Vercel deployment request failed "
        f"after {retries} attempts: "
        f"{last_error}"
    )


# =========================================================
# RUN GIT COMMAND
# =========================================================

def run_git(
    project_path,
    args,
):
    result = subprocess.run(
        [
            "git",
            *args,
        ],
        cwd=project_path,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "Git command failed."
        )

    return result.stdout.strip()


# =========================================================
# CHECK SECRET FILES
# =========================================================

def check_sensitive_files(
    project_path,
):
    """
    Detect obvious secret files before Git commit.
    """

    sensitive_names = {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "credentials.json",
        "service-account.json",
    }

    found = []

    for name in sensitive_names:

        if (
            project_path / name
        ).exists():

            found.append(name)

    return found


# =========================================================
# ENSURE SAFE GITIGNORE
# =========================================================

def ensure_gitignore(
    project_path,
):
    """
    Ensure common secret/build files are ignored.
    """

    gitignore_path = (
        project_path / ".gitignore"
    )

    required_entries = [
        ".env",
        ".env.*",
        "!.env.example",
        "credentials.json",
        "service-account.json",
        ".deployx/",
        ".vercel/",
        "__pycache__/",
        "*.pyc",
        "node_modules/",
        "dist/",
        "build/",
        ".venv/",
        "venv/",
    ]

    existing = set()

    if gitignore_path.exists():

        try:

            existing = {
                line.strip()
                for line in (
                    gitignore_path
                    .read_text(
                        encoding="utf-8"
                    )
                    .splitlines()
                )
                if line.strip()
            }

        except OSError:
            pass

    missing = [
        entry
        for entry in required_entries
        if entry not in existing
    ]

    if missing:

        with gitignore_path.open(
            "a",
            encoding="utf-8",
        ) as file:

            if (
                gitignore_path.exists()
                and gitignore_path.stat().st_size > 0
            ):

                file.write("\n")

            file.write(
                "\n".join(missing)
                + "\n"
            )

        print(
            "✓ Safe .gitignore rules ensured"
        )


# =========================================================
# PUSH PROJECT TO GITHUB
# =========================================================

def push_to_github(
    project_path,
    clone_url,
    credentials,
):
    """
    Initialize/configure Git and push the local project
    to the authenticated user's GitHub repository.

    GitHub credentials are supplied through Git ASKPASS.
    The access token is never placed inside the remote URL.
    """

    if not credentials:

        raise RuntimeError(
            "GitHub authentication credentials "
            "are not available."
        )

    if not clone_url:

        raise RuntimeError(
            "GitHub clone URL is missing."
        )

    if (
        "@" in clone_url
        and "github.com" in clone_url
    ):

        raise RuntimeError(
            "Unsafe GitHub clone URL detected."
        )

    print()
    print(
        "🐙 Preparing Git repository..."
    )

    # -----------------------------------------------------
    # SECRET SAFETY
    # -----------------------------------------------------

    sensitive_files = (
        check_sensitive_files(
            project_path
        )
    )

    if sensitive_files:

        print()
        print(
            "🔐 Sensitive files detected:"
        )

        for filename in sensitive_files:

            print(
                f"   • {filename}"
            )

        print()
        print(
            "✓ These files will be excluded "
            "from Git using .gitignore."
        )

    ensure_gitignore(
        project_path
    )

    # -----------------------------------------------------
    # INITIALIZE GIT
    # -----------------------------------------------------

    git_folder = (
        project_path / ".git"
    )

    if not git_folder.exists():

        run_git(
            project_path,
            ["init"],
        )

        print(
            "✓ Git repository initialized"
        )

    else:

        print(
            "✓ Existing Git repository detected"
        )

    # -----------------------------------------------------
    # MAIN BRANCH
    # -----------------------------------------------------

    run_git(
        project_path,
        [
            "branch",
            "-M",
            "main",
        ],
    )

    # -----------------------------------------------------
    # CONFIGURE ORIGIN
    # -----------------------------------------------------

    # Reuse an existing origin when it already points at this repository,
    # so a redeploy never rewrites working git config. Any other origin is
    # replaced with the authenticated user's repository.
    try:

        existing_origin = run_git(
            project_path,
            [
                "remote",
                "get-url",
                "origin",
            ],
        ).strip()

    except RuntimeError:
        existing_origin = ""

    if existing_origin == clone_url:

        print(
            "✓ GitHub remote already configured"
        )

    else:

        try:

            run_git(
                project_path,
                [
                    "remote",
                    "remove",
                    "origin",
                ],
            )

        except RuntimeError:
            pass

        # IMPORTANT:
        # Store only the normal HTTPS URL.
        # No token is stored in .git/config.

        run_git(
            project_path,
            [
                "remote",
                "add",
                "origin",
                clone_url,
            ],
        )

        print(
            "✓ GitHub remote configured"
        )

    # -----------------------------------------------------
    # STAGE
    # -----------------------------------------------------

    run_git(
        project_path,
        [
            "add",
            ".",
        ],
    )

    print(
        "✓ Files staged"
    )

    # -----------------------------------------------------
    # COMMIT
    # -----------------------------------------------------

    try:

        run_git(
            project_path,
            [
                "commit",
                "-m",
                "DeployX deployment",
            ],
        )

        print(
            "✓ Commit created"
        )

    except RuntimeError as exc:

        error_text = str(exc).lower()

        if (
            "nothing to commit"
            in error_text
            or "nothing added to commit"
            in error_text
        ):

            print(
                "✓ Nothing new to commit"
            )

        else:

            raise

    # -----------------------------------------------------
    # PUSH
    # -----------------------------------------------------

    print()
    print(
        "⬆️ Pushing project to GitHub..."
    )

    with tempfile.TemporaryDirectory(
        prefix="deployx-git-"
    ) as temp_directory:

        environment = (
            _git_authenticated_environment(
                credentials,
                temp_directory,
            )
        )

        push_result = subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "push",
                "-u",
                "origin",
                "main",
            ],
            cwd=project_path,
            capture_output=True,
            text=True,
            env=environment,
        )

    if push_result.returncode != 0:

        error_message = (
            push_result.stderr.strip()
            or push_result.stdout.strip()
            or "Git push failed."
        )

        if (
            "authentication failed"
            in error_message.lower()
            or "invalid credentials"
            in error_message.lower()
            or "403"
            in error_message
        ):

            raise RuntimeError(
                "GitHub authentication failed. "
                "Run `deployx login` and try again."
            )

        raise RuntimeError(
            error_message
        )

    print(
        "✓ Project pushed to GitHub"
    )


# =========================================================
# VERIFY GITHUB PUSH
# =========================================================

def verify_github_push(
    project_path,
    clone_url,
    credentials,
):
    """
    Verify that the exact local HEAD commit exists
    on GitHub main branch.
    """

    if not credentials:

        raise RuntimeError(
            "GitHub authentication credentials "
            "are not available."
        )

    if not clone_url:

        raise RuntimeError(
            "GitHub clone URL is missing."
        )

    # -----------------------------------------------------
    # LOCAL COMMIT
    # -----------------------------------------------------

    local_sha = run_git(
        project_path,
        [
            "rev-parse",
            "HEAD",
        ],
    ).strip()

    if not local_sha:

        raise RuntimeError(
            "Could not determine local Git commit."
        )

    # -----------------------------------------------------
    # REMOTE VERIFICATION
    # -----------------------------------------------------

    with tempfile.TemporaryDirectory(
        prefix="deployx-git-"
    ) as temp_directory:

        environment = (
            _git_authenticated_environment(
                credentials,
                temp_directory,
            )
        )

        result = subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                clone_url,
                "refs/heads/main",
            ],
            cwd=project_path,
            capture_output=True,
            text=True,
            env=environment,
        )

    if result.returncode != 0:

        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "Could not verify GitHub push."
        )

    output = result.stdout.strip()

    if not output:

        raise RuntimeError(
            "GitHub main branch could not be verified."
        )

    remote_sha = (
        output
        .split()[0]
        .strip()
    )

    if local_sha != remote_sha:

        raise RuntimeError(
            "GitHub push verification failed.\n"
            f"Local commit : {local_sha}\n"
            f"Remote commit: {remote_sha}"
        )

    print(
        "✓ GitHub push verified"
    )

    print(
        f"   Commit: {local_sha}"
    )

    return local_sha


# =========================================================
# DEPLOY
# =========================================================

def deploy():

    project_path = (
        Path.cwd().resolve()
    )

    api_url = os.getenv(
        "DEPLOYX_API_URL",
        "http://127.0.0.1:5000",
    )

    print_header()

    print(
        f"📁 Project: {project_path}"
    )

    # -----------------------------------------------------
    # ANALYZE
    # -----------------------------------------------------

    print()
    print(
        "🔍 Analyzing project..."
    )

    info = analyze(
        project_path
    )

    framework = info.get(
        "framework",
        "unknown",
    )

    project_type = info.get(
        "project_type",
        "unknown",
    )

    print()
    print(
        f"Project type : {project_type}"
    )

    print(
        f"Framework    : {framework}"
    )

    if info.get(
        "entry_file"
    ):

        print(
            f"Entry file   : "
            f"{info['entry_file']}"
        )

    if info.get(
        "package_manager"
    ):

        print(
            f"Package mgr  : "
            f"{info['package_manager']}"
        )

    # -----------------------------------------------------
    # PACKAGE ERROR
    # -----------------------------------------------------

    if (
        info.get("package_error")
        and framework not in {
            "static-html",
            "flask",
        }
    ):

        print(
            f"\n✗ {info['package_error']}"
        )

        return 1

    # -----------------------------------------------------
    # SUPPORTED CHECK
    # -----------------------------------------------------

    if not info.get(
        "supported"
    ):

        print(
            "\n✗ Project type/framework "
            "is not supported."
        )

        print()
        print(
            "Currently supported:"
        )

        print(
            "  • Static HTML"
        )

        print(
            "  • Flask / Python"
        )

        print(
            "  • React + Vite"
        )

        print(
            "  • Next.js"
        )

        print(
            "  • Vue + Vite"
        )

        return 1

    print(
        "✓ Project type supported"
    )

    # -----------------------------------------------------
    # ENVIRONMENT FILES
    # -----------------------------------------------------

    if info.get(
        "env_files"
    ):

        print()
        print(
            "🔐 Environment files detected:"
        )

        for name in info[
            "env_files"
        ]:

            print(
                f"   • {name}"
            )

        print(
            "   Variable names only will be "
            "inspected. Secret values are never sent."
        )

        if info.get(
            "env_variable_names"
        ):

            print()
            print(
                "   Environment variable names:"
            )

            for name in info[
                "env_variable_names"
            ]:

                print(
                    f"   • {name}"
                )

    # -----------------------------------------------------
    # AUTHENTICATION (GitHub + Vercel)
    # -----------------------------------------------------

    print()

    try:

        auth = ensure_authenticated()

    except Exception as exc:

        print()
        print(f"✗ Authentication failed: {exc}")

        return 1

    github_credentials = auth.get("github")
    vercel_credentials = auth.get("vercel")

    if not github_credentials:

        print()
        print("✗ GitHub authorization failed.")

        return 1

    if not vercel_credentials:

        print()
        print("✗ Vercel authorization failed.")

        return 1

    github_username = (
        github_credentials.get("username")
        or ""
    )

    # -----------------------------------------------------
    # STATIC HTML
    # -----------------------------------------------------

    if framework == "static-html":

        print()
        print(
            "🌐 Static HTML project detected."
        )

        print(
            "✓ No production build is required."
        )

        index_path = (
            project_path / "index.html"
        )

        if not index_path.exists():

            print(
                "✗ index.html was not found."
            )

            return 1

        print(
            "✓ index.html will be used "
            "as the website entry point."
        )

        build = {
            "success": True,
            "output": (
                "Static HTML project — "
                "build skipped."
            ),
        }

    # -----------------------------------------------------
    # FLASK
    # -----------------------------------------------------

    elif framework == "flask":

        print()
        print(
            "🐍 Flask project detected."
        )

        entry_file = info.get(
            "entry_file"
        )

        if not entry_file:

            print(
                "✗ Flask entry file "
                "was not detected."
            )

            return 1

        entry_path = (
            project_path / entry_file
        )

        if not entry_path.exists():

            print(
                f"✗ Entry file not found: "
                f"{entry_file}"
            )

            return 1

        print(
            f"✓ Entry file found: "
            f"{entry_file}"
        )

        print(
            "✓ Python build step is not "
            "required in Phase 1."
        )

        build = {
            "success": True,
            "output": (
                "Flask project — "
                "build validation skipped."
            ),
        }

    # -----------------------------------------------------
    # JAVASCRIPT PROJECTS
    # -----------------------------------------------------

    else:

        if not info.get(
            "package_json_found"
        ):

            print(
                "\n✗ package.json is required."
            )

            return 1

        # A missing `build` script is only a warning: Vercel runs the real
        # production build in the cloud, so a local script must never block
        # `deployx deploy`.
        if not info.get(
            "has_build_script"
        ):

            print(
                "\n⚠️  No `build` script was found in package.json."
            )

            print(
                "   Continuing - Vercel performs the real "
                "production build."
            )

            build = {
                "success": True,
                "output": (
                    "No local build script - "
                    "Vercel builds in the cloud."
                ),
            }

        else:

            print(
                f"✓ Package manager: "
                f"{info.get('package_manager', 'unknown')}"
            )

            print(
                f"✓ Framework: "
                f"{info.get('framework', 'unknown')}"
            )

            print(
                "✓ Build script found"
            )

            print()
            print(
                "🔨 Running production build..."
            )

            build = run_build(
                project_path,
                info["build_command"],
            )

    # -----------------------------------------------------
    # BUILD RESULT
    # -----------------------------------------------------

    if not build[
            "success"
    ]:

        print()
        print(
            "⚠️  Local build check could not run."
        )

        print(
            "─" * 64
        )

        print(
            build.get(
                "output",
                "Unknown error",
            )
        )

        print(
            "─" * 64
        )

        # This is NOT the deployment. Vercel installs the dependencies
        # and runs the real production build in the cloud, so a machine
        # without node_modules must never block a deployment. If the
        # project genuinely does not build, Vercel reports the failure
        # and DeployX explains it with Groq.
        print()
        print(
            "Continuing - Vercel performs the real "
            "production build."
        )

    else:

        print()
        print(
            "✓ Production build/validation successful"
        )

    print()
    print(
        f"✓ Output directory: "
        f"{info.get('output_directory', '.')}"
    )

    # -----------------------------------------------------
    # CREATE FLASK DEPLOYMENT RECORD
    # -----------------------------------------------------

    payload = {
        "project_name": info[
            "project_name"
        ],
        "framework": info[
            "framework"
        ],
        "project_type": project_type,
        "build_command": info.get(
            "build_command"
        ),
        "output_directory": info.get(
            "output_directory",
            ".",
        ),
    }

    # The dashboard record only feeds the local UI. It is NOT part of the
    # deployment itself, so a missing Flask server must never stop the
    # real GitHub -> Vercel pipeline.

    print()

    deployment_id = None

    try:

        result = create_deployment(
            api_url,
            payload,
        )

        deployment = result[
            "deployment"
        ]

        deployment_id = deployment[
            "id"
        ]

        print(
            f"✓ Deployment ID: "
            f"DEP-{deployment_id:05d}"
        )

    except Exception:

        print(
            "   Dashboard not reachable at "
            f"{api_url}"
        )

        print(
            "   Continuing - the dashboard is optional "
            "and is not part of the deployment."
        )

        print(
            "   Start it later with: python app.py"
        )

    # -----------------------------------------------------
    # CREATE / REUSE THE USER'S GITHUB REPOSITORY
    # -----------------------------------------------------

    print()
    print("📦 Creating GitHub repository...")

    # Folder names can contain spaces or other characters GitHub rejects,
    # so always use a sanitized name for the repository and Vercel project.
    safe_project_name = sanitize_repo_name(info["project_name"])

    try:

        repository = ensure_github_repository(
            github_credentials.get("access_token"),
            github_username,
            safe_project_name,
            private=False,
            description="Created by DeployX",
        )

    except Exception as exc:

        print()
        print(f"✗ Repository creation failed: {exc}")

        return 1

    repository_name = (
        repository.get("name")
        or safe_project_name
    )

    repository_url = repository.get("html_url")
    clone_url = repository.get("clone_url")

    if repository.get("full_name"):
        print(
            f"✓ GitHub repository ready: "
            f"{repository.get('full_name')}"
        )
    else:
        print(
            "✓ GitHub repository ready"
        )

    print(
        f"✓ Repository URL: "
        f"{repository_url}"
    )

    # -----------------------------------------------------
    # CALL n8n — REGISTER DEPLOYMENT / BACKEND WORKFLOW
    # -----------------------------------------------------

    n8n_context = {
        "github_username": github_username,
        "github_repository": repository.get("full_name"),
        "github_url": repository_url,
        "repository_name": repository_name,
        "clone_url": clone_url,
    }

    # -----------------------------------------------------
    # OPTIONAL n8n NOTIFICATION
    # -----------------------------------------------------
    #
    # The DeployX backend + n8n workflow are an OPTIONAL extra that can post
    # deployment events to an automation platform. They are never required for
    # the GitHub -> Vercel pipeline: the CLI already holds the user's own
    # tokens and talks to both providers directly.
    backend_ticket = None

    if backend_client.is_available():

        print()
        print(
            "✓ DeployX backend: connected"
        )

        try:

            backend_ticket = prepare_deployment_ticket(
                github_credentials.get("access_token"),
                vercel_credentials.get("access_token"),
                deployment_id=(
                    f"DEP-{deployment_id:05d}"
                    if deployment_id is not None
                    else "DEP-unknown"
                ),
                project_name=safe_project_name,
                repository=f"{github_username}/{repository_name}",
            )

        except Exception:
            # Optional: a ticket failure must not stop a deployment the
            # CLI can already perform itself.
            backend_ticket = None

        if backend_ticket:
            n8n_context["deployment_ticket"] = backend_ticket
            print(
                "✓ Deployment ticket created"
            )

    else:

        print()
        print(
            "⚠️  DeployX backend not running - continuing without"
        )
        print(
            "   the optional n8n notification."
        )

    if backend_ticket:

        print()
        print(
            "⚙️ Sending deployment to n8n..."
        )

    else:

        print()
        print(
            "n8n workflow not configured; "
            "continuing without AI change analysis."
        )

    if backend_ticket:

        try:

            n8n_result = call_n8n(
                info,
                deployment_id,
                n8n_context,
            )

            if not n8n_result.get(
                "success"
            ):

                print(
                    "⚠️  n8n did not accept the request"
                )

                print(
                    "   Continuing - deployment does not "
                    "depend on n8n."
                )

                print()
                print(
                    f"✓ GitHub repository: "
                    f"{repository_name}"
                )

                print(
                    f"✓ Repository URL: "
                    f"{repository_url}"
                )

            else:

                # Only trust n8n's repository when it belongs to the
                # authenticated user; otherwise keep the user's own repo so
                # the deployment never targets a shared account.
                n8n_repository_url = n8n_result.get(
                    "repository_url"
                )

                n8n_clone_url = n8n_result.get(
                    "clone_url"
                )

                if (
                    n8n_repository_url
                    and github_username
                    and f"/{github_username}/" in n8n_repository_url
                ):

                    repository_url = n8n_repository_url

                    repository_name = (
                        n8n_result.get("repository_name")
                        or repository_name
                    )

                    clone_url = n8n_clone_url or clone_url

                print(
                    f"✓ GitHub repository: "
                    f"{repository_name}"
                )

                print(
                    f"✓ Repository URL: "
                    f"{repository_url}"
                )

        except Exception as exc:

            # Optional integration only: a webhook problem must never stop a
            # deployment the CLI performs directly against GitHub and Vercel.
            print(
                f"⚠️  n8n notification failed: "
                f"{exc}"
            )

            print(
                "   Continuing - deployment does not "
                "depend on n8n."
            )

    # -----------------------------------------------------
    # PUSH TO GITHUB
    # -----------------------------------------------------

    try:

        push_to_github(
            project_path,
            clone_url,
            github_credentials,
        )

    except Exception as exc:

        print()
        print(
            f"✗ GitHub push failed: "
            f"{exc}"
        )

        return 1

    # -----------------------------------------------------
    # VERIFY GITHUB PUSH
    # -----------------------------------------------------

    try:

        commit_sha = verify_github_push(
            project_path,
            clone_url,
            github_credentials,
        )

    except Exception as exc:

        print()
        print(
            f"✗ GitHub push verification failed: "
            f"{exc}"
        )

        return 1

    # -----------------------------------------------------
    # VERCEL: REAL PROJECT + REAL DEPLOYMENT + MONITORING
    # -----------------------------------------------------
    #
    # The CLI holds the user's own Vercel token, so it talks to Vercel
    # directly. Vercel is the ONLY source of truth for deployment status:
    # SUCCESS is reported only when Vercel itself returns READY.
    print()
    print(
        "✓ Code pushed to GitHub"
    )

    print()
    print(
        "▲ Creating or reusing the Vercel project..."
    )

    vercel_token = vercel_credentials.get("access_token")
    vercel_project_name = repository_name

    try:

        vercel_project = ensure_vercel_project(
            vercel_token,
            vercel_project_name,
            framework=info.get("framework"),
        )

        if vercel_project.get("created"):
            print(
                f"✓ Vercel project created: "
                f"{vercel_project_name}"
            )
        else:
            print(
                f"✓ Vercel project reused: "
                f"{vercel_project_name}"
            )

    except VercelError as exc:
        print()
        print(
            f"✗ {exc.message}"
        )
        return 1

    print()
    print(
        "▲ Starting Vercel deployment..."
    )

    try:

        vercel_deployment = create_vercel_deployment(
            vercel_token,
            vercel_project_name,
            project_path,
            framework=info.get("framework"),
        )

        vercel_deployment_id = vercel_deployment.get("id")

        if not vercel_deployment_id:
            raise VercelError(
                "invalid_response",
                "Vercel did not return a deployment id.",
            )

        print(
            "✓ Vercel deployment started"
        )
        print(
            f"  Deployment ID: "
            f"{vercel_deployment_id}"
        )

    except VercelError as exc:
        print()
        print(
            f"✗ Vercel deployment failed: "
            f"{exc.message}"
        )
        return 1

    # -----------------------------------------------------
    # MONITOR THE REAL DEPLOYMENT
    # -----------------------------------------------------
    #
    # Every state printed here came from a real Vercel API response.
    print()
    print(
        "▲ Monitoring deployment..."
    )

    def _on_update(status, ready_state, _payload):
        if status == "BUILDING":
            print(
                f"  Building... ({ready_state})"
            )
        elif status == "PENDING":
            print(
                f"  Pending... ({ready_state})"
            )

    try:

        vercel_outcome = poll_deployment(
            vercel_token,
            vercel_deployment_id,
            on_update=_on_update,
        )

    except VercelError as exc:
        print()
        print(
            f"✗ {exc.message}"
        )
        return 1

    # -----------------------------------------------------
    # DEPLOYMENT FAILED - ASK GROQ TO EXPLAIN IT
    # -----------------------------------------------------
    #
    # Vercel already reported the failure above. Groq only explains it
    # in beginner-friendly language. Groq never decides the outcome, and
    # an AI outage must never hide the real error.
    if vercel_outcome.get("status") != "SUCCESS":

        print()
        print(
            "✗ Deployment failed"
        )

        real_error = (
            vercel_outcome.get("error")
            or "Vercel reported a build error."
        )

        print(
            f"  Vercel error: {real_error}"
        )

        _run_groq_analysis(
            info,
            real_error,
        )

        return 1

    # -----------------------------------------------------
    # FINAL RESULT (URLs come from the real API responses)
    # -----------------------------------------------------
    vercel_url = vercel_outcome.get("url")

    # Vercel said READY, but a live URL is still required for this to
    # count as a completed deployment. Never report success without one.
    if not vercel_url:
        print()
        print(
            "✗ Vercel did not return a live URL."
        )
        return 1

    if deployment_id is not None:
        _update_deployment_record(
            deployment_id,
            vercel_url,
            repository_url,
        )

    print()
    line()

    print(
        "DeployX"
    )

    line()

    print(
        "✓ Project detected"
    )
    print(
        f"   Project:  {info['project_name']}"
    )
    print(
        f"   Framework: {info.get('framework') or 'unknown'}"
    )

    print(
        "✓ GitHub repository created/found"
    )
    print(
        f"   {repository_url}"
    )

    print(
        "✓ Code pushed to GitHub"
    )

    print(
        "✓ Vercel deployment started"
    )

    print(
        "✓ Deployment completed"
    )
    print(
        f"   Vercel Deployment ID: {vercel_deployment_id}"
    )

    print()
    print(
        f"✓ Live URL: {vercel_url}"
    )

    line()
    print(
        "Deployment: SUCCESS"
    )
    line()
    print()

    return 0

# =========================================================
# SCAN
# =========================================================

def _run_groq_analysis(info, real_error):
    """
    Ask Groq to explain a REAL Vercel failure, then print it clearly.

    The raw Vercel error is always printed first by the caller, so the
    developer sees the true reason before any AI wording. If Groq is not
    configured or is unreachable, DeployX says so and stops - it never
    invents an explanation.
    """
    print()
    print(
        "🤖 DeployX AI:"
    )

    if not groq_is_configured():
        print(
            "   Groq AI analysis is not configured."
        )
        print(
            "   Set GROQ_API_KEY in your .env file to"
        )
        print(
            "   get an explanation."
        )
        return

    try:

        analysis = groq_analyze_build_error(
            project=info["project_name"],
            framework=info.get("framework") or "unknown",
            error=real_error,
        )

    except GroqError as exc:

        print(f"   {exc.message}")
        return

    if not analysis:
        print(
            "   No analysis was returned."
        )
        return

    print(
        f"   What went wrong: "
        f"{analysis['summary']}"
    )

    print(
        f"   Why it happened: "
        f"{analysis['cause']}"
    )

    if analysis["affected_files"]:
        print(
            "   Likely file(s): "
            + ", ".join(analysis["affected_files"])
        )

    if analysis["recommended_fix"]:
        print(
            f"   What to change: "
            f"{analysis['recommended_fix']}"
        )

    for command in analysis["commands"]:
        print(f"   Run: {command}")

    print(
        f"   Confidence: "
        f"{analysis['confidence']}"
    )


def _update_deployment_record(deployment_id, vercel_url, github_url):
    """
    Store the real result in the local dashboard record.

    Best effort only: the dashboard is an optional extra and must never
    turn a successful deployment into a failure.
    """
    try:

        update_deployment(
            deployment_id,
            status="SUCCESS",
            vercel_url=vercel_url,
            github_url=github_url,
        )

    except Exception:

        pass


def scan():

    info = analyze(
        Path.cwd()
    )

    print(
        json.dumps(
            info,
            indent=2,
        )
    )

    return 0


# =========================================================
# LOGIN
# =========================================================

def login(provider=None):

    print_header()

    provider = (provider or "").lower()

    try:

        if provider == "github":

            credentials = github_login()

            if credentials:
                return 0

            print("✗ GitHub login did not complete.")

            return 1

        if provider == "vercel":

            credentials = vercel_login()

            if credentials:
                return 0

            print("✗ Vercel login did not complete.")

            return 1

        result = ensure_authenticated()

        if result.get("github") and result.get("vercel"):

            print()
            print("✓ DeployX authentication complete")

            return 0

        print()
        print("✗ DeployX authentication did not complete.")

        return 1

    except Exception as exc:

        print(
            f"✗ Login failed: {exc}"
        )

        return 1


# =========================================================
# LOGOUT
# =========================================================

def logout_command(provider=None):

    print_header()

    try:

        logout(provider)

        return 0

    except Exception as exc:

        print(
            f"✗ Logout failed: {exc}"
        )

        return 1


# =========================================================
# AUTH STATUS
# =========================================================

def auth_status():

    print_header()

    print_auth_status()

    return 0


# =========================================================
# MAIN
# =========================================================

def main():

    configure_console_encoding()

    parser = argparse.ArgumentParser(
        prog="deployx",
        description=(
            "DeployX — one-command "
            "deployment platform"
        ),
    )

    subparsers = (
        parser.add_subparsers(
            dest="command"
        )
    )

    subparsers.add_parser(
        "deploy",
        help=(
            "Analyze and deploy "
            "the current project"
        ),
    )

    subparsers.add_parser(
        "scan",
        help=(
            "Print project detection "
            "information"
        ),
    )

    login_parser = subparsers.add_parser(
        "login",
        help=(
            "Connect DeployX to GitHub and Vercel"
        ),
    )

    login_parser.add_argument(
        "provider",
        nargs="?",
        choices=["github", "vercel"],
        help="Only connect a single provider",
    )

    logout_parser = subparsers.add_parser(
        "logout",
        help=(
            "Disconnect DeployX authentication"
        ),
    )

    logout_parser.add_argument(
        "provider",
        nargs="?",
        choices=["github", "vercel"],
        help="Only disconnect a single provider",
    )

    subparsers.add_parser(
        "auth-status",
        help=(
            "Show DeployX authentication status"
        ),
    )

    args = parser.parse_args()

    if args.command == "deploy":

        raise SystemExit(
            deploy()
        )

    if args.command == "scan":

        raise SystemExit(
            scan()
        )

    if args.command == "login":

        raise SystemExit(
            login(getattr(args, "provider", None))
        )

    if args.command == "logout":

        raise SystemExit(
            logout_command(getattr(args, "provider", None))
        )

    if args.command == "auth-status":

        raise SystemExit(
            auth_status()
        )

    parser.print_help()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    main()
