"""Shared fixtures for the DeployX backend test-suite."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Configuration must be in place BEFORE backend modules are imported.
# backend/config.py treats the real .env as authoritative (override=True), so
# test values are injected by rebinding the already-resolved config module
# instead of relying on os.environ ordering.
_TMPDIR = tempfile.mkdtemp(prefix="deployx-backend-tests-")
os.environ["DEPLOYX_DATABASE_URL"] = (
    f"sqlite:///{(Path(_TMPDIR) / 'default.db').as_posix()}"
)

# DeployX-01 built-in defaults must be exercised, never an override that
# happens to exist in the developer's shell.
for _var in ("DEPLOYX_N8N_WEBHOOK_URL", "DEPLOYX_N8N_VERCEL_WEBHOOK_URL"):
    os.environ.pop(_var, None)

from backend import config as backend_config  # noqa: E402

SERVICE_KEY = "test-service-key-abc123"

# Rebind resolved configuration to isolated test values so the developer's
# real secrets are never used (or leaked) by the test-suite.
backend_config.N8N_SERVICE_KEY = SERVICE_KEY
backend_config.ENCRYPTION_KEY = Fernet.generate_key().decode("ascii")

ALICE_GITHUB_TOKEN = "ghp_ALICE_TOKEN_MUST_NEVER_LEAK"
ALICE_VERCEL_TOKEN = "vcx_ALICE_TOKEN_MUST_NEVER_LEAK"
BOB_GITHUB_TOKEN = "ghp_BOB_TOKEN_MUST_NEVER_LEAK"

SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_KEY}"}


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeGitHub:
    """Minimal GitHub API double that records the auth header it receives."""

    def __init__(self, username="alice", user_id=1):
        self.username = username
        self.user_id = user_id
        self.repos = {}
        self.calls = []
        self.fail_with = None

    def _maybe_fail(self):
        if self.fail_with is not None:
            return FakeResponse(self.fail_with, {"message": "boom"})
        return None

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, dict(headers or {})))
        failure = self._maybe_fail()
        if failure:
            return failure
        if url.endswith("/user"):
            return FakeResponse(200, {"id": self.user_id, "login": self.username})
        full_name = "/".join(url.split("/repos/")[-1].split("/"))
        if full_name in self.repos:
            return FakeResponse(200, self.repos[full_name])
        return FakeResponse(404, {"message": "Not Found"})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, dict(headers or {})))
        failure = self._maybe_fail()
        if failure:
            return failure
        if url.endswith("/user/repos"):
            full_name = f"{self.username}/{json['name']}"
            if full_name in self.repos:
                return FakeResponse(422, {"message": "already exists"})
            payload = {
                # Vercel's create-deployment API requires the NUMERIC
                # repository id, which backend/vercel.py resolves through
                # the GitHub API. Without it the deployment legitimately
                # fails with 502.
                "id": 1000 + len(self.repos),
                "name": json["name"],
                "full_name": full_name,
                "owner": {"login": self.username},
                "private": bool(json.get("private")),
                "html_url": f"https://github.com/{full_name}",
                "clone_url": f"https://github.com/{full_name}.git",
                "default_branch": "main",
            }
            self.repos[full_name] = payload
            return FakeResponse(201, payload)
        return FakeResponse(500, {"message": "unexpected"})


class FakeVercel:
    def __init__(self, username="alice", user_id="u-alice", team_id="team-alice"):
        self.username = username
        self.user_id = user_id
        self.team_id = team_id
        self.calls = []
        self.fail_with = None
        self.ready_state = "READY"
        self.project_id = "prj_test_1"

    def _maybe_fail(self):
        if self.fail_with is not None:
            return FakeResponse(self.fail_with, {"message": "boom"})
        return None

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, dict(headers or {})))
        failure = self._maybe_fail()
        if failure:
            return failure
        if url.endswith("/v2/user"):
            return FakeResponse(
                200,
                {
                    "user": {
                        "id": self.user_id,
                        "username": self.username,
                        "email": f"{self.username}@example.com",
                    },
                    "teamId": self.team_id,
                    "team": {"id": self.team_id, "slug": self.team_id},
                },
            )
        if "/env" in url:
            return FakeResponse(
                200,
                {
                    "envs": [
                        {"key": "API_KEY", "value": "super-secret-value"},
                        {"key": "DATABASE_URL", "value": "postgres://secret"},
                    ]
                },
            )
        if "/deployments/" in url:
            return FakeResponse(
                200,
                {
                    "id": "dpl_1",
                    "name": "demo",
                    "projectId": self.project_id,
                    "url": "https://demo.vercel.app",
                    "readyState": self.ready_state,
                    "inspectorUrl": "https://vercel.com/inspector",
                },
            )
        return FakeResponse(404, {"message": "Not Found"})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, dict(headers or {})))
        failure = self._maybe_fail()
        if failure:
            return failure
        return FakeResponse(
            201,
            {
                "id": "dpl_1",
                "name": (json or {}).get("name"),
                "projectId": self.project_id,
                "url": "https://demo.vercel.app",
                "readyState": "BUILDING",
                "inspectorUrl": "https://vercel.com/inspector",
            },
        )


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from backend import config, db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'test.db'}")
    db.reset_engine()

    from backend.app import create_app

    application = create_app()
    yield application

    db.reset_engine()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def github(monkeypatch):
    from backend import github as backend_github

    fake = FakeGitHub()
    monkeypatch.setattr(backend_github, "requests", fake)
    return fake


@pytest.fixture()
def vercel(monkeypatch):
    from backend import vercel as backend_vercel

    fake = FakeVercel()
    monkeypatch.setattr(backend_vercel, "requests", fake)
    return fake


@pytest.fixture()
def alice(client, monkeypatch):
    """Create a DeployX session for Alice (provider APIs are stubbed)."""
    from services import github_service, vercel_service

    def fake_github_user(token):
        if token == ALICE_GITHUB_TOKEN:
            return {"id": 1, "login": "alice"}
        raise RuntimeError("invalid github token")

    def fake_vercel_user(token):
        if token == ALICE_VERCEL_TOKEN:
            return {
                "username": "alice@example.com",
                "email": "alice@example.com",
                "sub": "u-alice",
            }
        return None

    monkeypatch.setattr(github_service, "get_authenticated_user", fake_github_user)
    monkeypatch.setattr(vercel_service, "get_authenticated_user", fake_vercel_user)

    response = client.post(
        "/v1/session",
        json={
            "github_token": ALICE_GITHUB_TOKEN,
            "vercel_token": ALICE_VERCEL_TOKEN,
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    return {
        "session_token": payload["session_token"],
        "user": payload["user"],
        "session_response": payload,
    }


def issue_ticket(client, session_token, **overrides):
    body = {
        "deployment_id": "DEP-00001",
        "project_name": "demo",
        "repository": "alice/demo",
    }
    body.update(overrides)
    return client.post(
        "/v1/deployment-ticket",
        json=body,
        headers={"Authorization": f"Bearer {session_token}"},
    )
