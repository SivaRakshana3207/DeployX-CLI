from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli import credential_store
from config import (
    DEPLOYX_VERCEL_CLIENT_ID,
    VERCEL_DEVICE_AUTHORIZATION_ENDPOINT,
    VERCEL_SCOPES,
    VERCEL_TOKEN_ENDPOINT,
)
from services import github_service, vercel_service


APP_NAME = "DeployX"

GITHUB_CLIENT_ID = "Ov23lijBOw7v2jtZM5sp"

GITHUB_DEVICE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com"


# ============================================================
# Local credential storage (secure store, see credential_store.py)
# ============================================================

def save_provider_credentials(provider: str, credentials: dict[str, Any]) -> None:
    credential_store.set_provider(provider, credentials)


def get_provider_credentials(provider: str) -> dict[str, Any] | None:
    return credential_store.get_provider(provider)


def is_authenticated(provider: str) -> bool:
    credentials = credential_store.get_provider(provider)
    if not credentials:
        return False
    return bool(credentials.get("access_token"))


def get_github_username() -> str | None:
    return (get_provider_credentials("github") or {}).get("username")


def get_vercel_username() -> str | None:
    return (get_provider_credentials("vercel") or {}).get("username")


def storage_backend() -> str:
    """Human-readable description of the credential storage backend."""
    return credential_store.backend_label()


# ============================================================
# Credential validation (against the provider APIs)
# ============================================================

def verify_github_credentials(credentials: dict[str, Any] | None) -> bool:
    token = (credentials or {}).get("access_token")
    if not token:
        return False
    return github_service.validate_token(token)


def verify_vercel_credentials(credentials: dict[str, Any] | None) -> bool:
    token = (credentials or {}).get("access_token")
    if not token:
        return False
    return vercel_service.validate_token(token)


# ============================================================
# GitHub authentication (OAuth Device Flow)
# ============================================================

def github_login() -> dict[str, Any] | None:
    """
    Authenticate the current user against GitHub.

    Returns the saved credentials dict on success, otherwise None.
    """
    print()
    print("DeployX GitHub Login")
    print("--------------------")

    print("Requesting GitHub device code...")

    try:
        response = requests.post(
            GITHUB_DEVICE_URL,
            params={"client_id": GITHUB_CLIENT_ID, "scope": "repo"},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"✗ Failed to contact GitHub: {exc}")
        return None

    data = response.json()

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri")
    expires_in = int(data.get("expires_in", 900))
    interval = int(data.get("interval", 5))

    if not device_code or not user_code:
        print("✗ GitHub did not return a valid device code.")
        return None

    print()
    print("Open this URL in your browser:")
    print()
    print(f"  {verification_uri}")
    print()
    print("Enter this code:")
    print()
    print(f"  {user_code}")
    print()

    try:
        webbrowser.open(verification_uri)
        print("✓ Browser opened.")
    except Exception:
        print("Could not open the browser automatically.")
        print("Please open the URL manually.")

    print()
    print("Waiting for GitHub authorization...")

    start_time = time.time()

    while time.time() - start_time < expires_in:
        time.sleep(interval)

        try:
            token_response = requests.post(
                GITHUB_TOKEN_URL,
                params={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": (
                        "urn:ietf:params:oauth:grant-type:device_code"
                    ),
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
        except requests.RequestException as exc:
            print(f"✗ GitHub request failed: {exc}")
            return None

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if access_token:
            try:
                user_data = github_service.get_authenticated_user(access_token)
            except requests.RequestException as exc:
                print(f"✗ Token received, but verification failed: {exc}")
                return None

            credentials = {
                "access_token": access_token,
                "token_type": token_data.get("token_type", "bearer"),
                "scope": token_data.get("scope", ""),
                "username": user_data.get("login"),
                "user_id": user_data.get("id"),
            }
            save_provider_credentials("github", credentials)

            print()
            print("✓ GitHub authentication successful!")
            if credentials["username"]:
                print(f"✓ GitHub account: {credentials['username']}")
            print()
            return credentials

        error = token_data.get("error")

        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue

        if error == "slow_down":
            interval = int(token_data.get("interval", interval + 5))
            print(".", end="", flush=True)
            continue

        if error == "access_denied":
            print()
            print("✗ GitHub authorization was denied.")
            return None

        if error in {"expired_token", "token_expired"}:
            print()
            print("✗ GitHub device code expired.")
            return None

        if error:
            print()
            print(f"✗ GitHub authentication failed: {error}")
            return None

    print()
    print("✗ GitHub authentication timed out.")
    return None


# ============================================================
# Vercel authentication (OAuth 2.0 Device Authorization Grant, RFC 8628)
# ============================================================

def vercel_login() -> dict[str, Any] | None:
    """
    Authenticate the current user against Vercel using the same
    browser-based device flow as the official Vercel CLI.

    Returns the saved credentials dict on success, otherwise None.
    """
    print()
    print("DeployX Vercel Login")
    print("--------------------")

    print("Requesting Vercel device code...")

    try:
        response = requests.post(
            VERCEL_DEVICE_AUTHORIZATION_ENDPOINT,
            data={
                "client_id": DEPLOYX_VERCEL_CLIENT_ID,
                "scope": VERCEL_SCOPES,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"✗ Failed to contact Vercel: {exc}")
        return None

    data = response.json()

    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri")
    verification_uri_complete = (
        data.get("verification_uri_complete") or verification_uri
    )
    expires_in = int(data.get("expires_in", 1800))
    interval = int(data.get("interval", 5))

    if not device_code or not user_code:
        print("✗ Vercel did not return a valid device code.")
        return None

    print()
    print("Open this URL in your browser:")
    print()
    print(f"  {verification_uri}")
    print()
    print("Enter this code:")
    print()
    print(f"  {user_code}")
    print()

    try:
        webbrowser.open(verification_uri_complete)
        print("✓ Browser opened.")
    except Exception:
        print("Could not open the browser automatically.")
        print("Please open the URL manually.")

    print()
    print("Waiting for Vercel authorization...")

    start_time = time.time()

    while time.time() - start_time < expires_in:
        time.sleep(interval)

        try:
            token_response = requests.post(
                VERCEL_TOKEN_ENDPOINT,
                data={
                    "client_id": DEPLOYX_VERCEL_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": (
                        "urn:ietf:params:oauth:grant-type:device_code"
                    ),
                },
                headers={"Accept": "application/json"},
                timeout=20,
            )
        except requests.RequestException as exc:
            print(f"✗ Vercel request failed: {exc}")
            return None

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if access_token:
            user = vercel_service.get_authenticated_user(access_token) or {}
            credentials = {
                "access_token": access_token,
                "token_type": token_data.get("token_type", "bearer"),
                "scope": token_data.get("scope", VERCEL_SCOPES),
                "refresh_token": token_data.get("refresh_token"),
                "username": user.get("username"),
                "email": user.get("email"),
            }
            save_provider_credentials("vercel", credentials)

            print()
            print("✓ Vercel authentication successful!")
            if credentials["username"]:
                print(f"✓ Vercel account: {credentials['username']}")
            print()
            return credentials

        error = token_data.get("error")

        if error == "authorization_pending":
            print(".", end="", flush=True)
            continue

        if error == "slow_down":
            interval += 5
            print(".", end="", flush=True)
            continue

        if error == "access_denied":
            print()
            print("✗ Vercel authorization was denied.")
            return None

        if error in {"expired_token", "token_expired"}:
            print()
            print("✗ Vercel device code expired.")
            return None

        if error:
            print()
            print(f"✗ Vercel authentication failed: {error}")
            return None

    print()
    print("✗ Vercel authentication timed out.")
    return None


# ============================================================
# Automatic authentication (used by `deployx deploy`)
# ============================================================

def _ensure_provider(provider, login_function, verify_function, label):
    """
    Ensure a single provider is authenticated and still valid.

    Reuses saved credentials, re-authorizes when they are missing or
    invalid, and returns the credentials dict (or None on failure).
    """
    credentials = get_provider_credentials(provider)

    if credentials and credentials.get("access_token"):
        if verify_function(credentials):
            username = credentials.get("username")
            suffix = f" ({username})" if username else ""
            print(f"✓ {label} authentication: connected{suffix}")
            return credentials

        print(f"✓ Invalid {label} authentication detected.")
        print(f"🔐 {label} reauthorization required...")
        result = login_function()
        if not result:
            print(f"✗ {label} authorization failed.")
            return None
        print(f"✓ {label} reauthorized")
        return result

    print(f"🔐 {label} authorization required...")
    result = login_function()
    if not result:
        print(f"✗ {label} authorization failed.")
        return None

    username = result.get("username")
    print(f"✓ {label} connected" + (f" as {username}" if username else ""))
    return result


def ensure_authenticated() -> dict[str, dict[str, Any] | None]:
    """
    Make sure the current user is authenticated with GitHub and Vercel.

    First run: starts the browser authorization for both providers and
    saves the credentials. Later runs: reuses the saved credentials.

    Returns ``{"github": credentials, "vercel": credentials}``.
    """
    github = _ensure_provider(
        "github", github_login, verify_github_credentials, "GitHub",
    )
    if github is None:
        return {"github": None, "vercel": None}

    vercel = _ensure_provider(
        "vercel", vercel_login, verify_vercel_credentials, "Vercel",
    )
    return {"github": github, "vercel": vercel}


# ============================================================
# Logout
# ============================================================

def logout(provider: str | None = None) -> None:
    print()

    if provider:
        credential_store.delete_provider(provider)
        print(f"✓ {provider.title()} authentication removed.")
    else:
        credential_store.delete_all()
        print("✓ All DeployX authentication removed.")


# ============================================================
# Authentication status
# ============================================================

def auth_status() -> dict[str, bool]:
    return {
        "github": is_authenticated("github"),
        "vercel": is_authenticated("vercel"),
    }


def print_auth_status() -> None:
    github = get_provider_credentials("github")
    vercel = get_provider_credentials("vercel")

    print()
    print("DeployX Authentication")
    print("----------------------")

    if github and github.get("access_token"):
        username = github.get("username")
        print("GitHub : ✓ Connected" + (f" ({username})" if username else ""))
    else:
        print("GitHub : ✗ Not connected")

    if vercel and vercel.get("access_token"):
        username = vercel.get("username")
        print("Vercel : ✓ Connected" + (f" ({username})" if username else ""))
    else:
        print("Vercel : ✗ Not connected")

    print()


# ============================================================
# CLI (python -m cli.auth ...)
# ============================================================

def main() -> None:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "status"

    if command in {"github", "vercel"}:
        result = github_login() if command == "github" else vercel_login()
        raise SystemExit(0 if result else 1)

    if command == "login":
        result = ensure_authenticated()
        ok = bool(result.get("github")) and bool(result.get("vercel"))
        raise SystemExit(0 if ok else 1)

    if command == "logout":
        provider = sys.argv[2].lower() if len(sys.argv) > 2 else None
        logout(provider)
        return

    if command in {"status", "auth-status"}:
        print_auth_status()
        return

    print("DeployX Authentication")
    print()
    print("Usage:")
    print("  python -m cli.auth github")
    print("  python -m cli.auth vercel")
    print("  python -m cli.auth login")
    print("  python -m cli.auth status")
    print("  python -m cli.auth logout [github|vercel]")


if __name__ == "__main__":
    main()
