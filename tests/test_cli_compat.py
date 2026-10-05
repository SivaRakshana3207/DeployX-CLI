"""
CLI compatibility tests.

These guard the CLI's transport and payload behaviour. The Phase 2 CLI talks
to the DeployX-02 MultiUser webhooks; the legacy DeployX-01 webhook paths are
never used.
"""

from __future__ import annotations

import io
import json
import urllib.request
from contextlib import redirect_stdout

DEPLOYX_02_N8N_WEBHOOK = (
    "https://siva327.app.n8n.cloud/webhook/deployx-mu"
)
DEPLOYX_02_VERCEL_WEBHOOK = (
    "https://siva327.app.n8n.cloud/webhook/deployx-mu-vercel"
)


# --- configuration regression guards ---------------------------------

def test_config_loads_root_env_not_backend_env():
    """
    The backend must load the PROJECT ROOT .env, resolved from this file's
    location, so it works from any working directory.
    """
    from pathlib import Path

    from backend import config

    root_env = Path(__file__).resolve().parents[1] / ".env"
    assert root_env.is_file(), "a project-root .env must exist for the backend"

    loaded = config._ENV_LOADED_FROM
    assert loaded is not None, "no .env was loaded at all"
    assert Path(loaded).resolve() == root_env.resolve(), (
        f"expected the root .env, got {loaded}"
    )


def test_config_reports_keys_present_without_revealing_them():
    from backend import config

    described = config.describe()
    assert described["encryption_key_present"] is True
    assert described["service_key_present"] is True
    # describe() must never leak key material.
    serialised = json.dumps(described)
    assert config.N8N_SERVICE_KEY not in serialised
    assert config.ENCRYPTION_KEY not in serialised


def test_config_does_not_fall_back_to_backend_env():
    """A stray backend/.env must not be required or created."""
    from pathlib import Path

    from backend import config

    assert not (Path(config.BASE_DIR) / ".env").exists(), (
        "backend/.env should not exist; the root .env is the single source"
    )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _capture(monkeypatch, body):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


# --- 14. existing CLI keeps working -------------------------------------

def test_cli_modules_import():
    import cli.analyzer  # noqa: F401
    import cli.api_client  # noqa: F401
    import cli.auth  # noqa: F401
    import cli.builder  # noqa: F401
    import cli.deployx  # noqa: F401
    import cli.git_manager  # noqa: F401
    import services.deployment_service  # noqa: F401
    import services.github_service  # noqa: F401
    import services.vercel_service  # noqa: F401
    import utils.project_scanner  # noqa: F401


def test_cli_auth_status_still_works(capsys):
    from cli import auth

    auth.print_auth_status()
    output = capsys.readouterr().out
    assert "DeployX Authentication" in output
    assert "GitHub" in output and "Vercel" in output


def test_call_n8n_vercel_never_forwards_provider_tokens(monkeypatch):
    """Even if a caller passes a token, it must not reach n8n."""
    from cli import deployx

    captured = _capture(
        monkeypatch, {"success": True, "vercel_url": "https://x.vercel.app"}
    )

    info = {
        "project_name": "demo",
        "framework": "static-html",
        "project_type": "static",
        "env_variable_names": ["API_KEY"],
    }

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = deployx.call_n8n_vercel(
            info,
            1,
            "https://github.com/alice/demo",
            "abc123",
            {
                "github_username": "alice",
                "github_repository": "alice/demo",
                "vercel_username": "alice@example.com",
                "vercel_token": "vcx_MUST_NOT_LEAK",
                "deployment_ticket": "ticket-123",
            },
            retries=1,
        )

    assert result["success"] is True
    payload = captured["payload"]
    serialised = json.dumps(payload)

    assert "vcx_MUST_NOT_LEAK" not in serialised
    assert "vercel_token" not in payload
    assert payload["deployment_ticket"] == "ticket-123"
    assert payload["github_username"] == "alice"
    assert captured["url"] == DEPLOYX_02_VERCEL_WEBHOOK
    assert "vcx_MUST_NOT_LEAK" not in buffer.getvalue()


def test_call_n8n_payload_carries_user_context(monkeypatch):
    from cli import deployx

    captured = _capture(monkeypatch, {"success": True})

    info = {
        "project_name": "demo",
        "framework": "react-vite",
        "project_type": "javascript",
        "build_command": "npm run build",
        "output_directory": "dist",
        "env_variable_names": ["API_KEY"],
    }

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        deployx.call_n8n(
            info,
            1,
            {
                "github_username": "alice",
                "github_repository": "alice/demo",
                "deployment_ticket": "ticket-123",
            },
            retries=1,
        )

    payload = captured["payload"]
    assert captured["url"] == DEPLOYX_02_N8N_WEBHOOK
    assert payload["github_username"] == "alice"
    assert payload["github_repository"] == "alice/demo"
    assert payload["env_var_names"] == ["API_KEY"]
    assert payload["branch"] == "main"
    assert payload["deployment_ticket"] == "ticket-123"
    assert "access_token" not in payload


def test_backend_client_is_graceful_when_backend_is_down(monkeypatch):
    from cli import backend_client

    def failing_get(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(backend_client.requests, "get", failing_get)

    assert backend_client.is_available() is False

    try:
        backend_client.prepare_deployment_ticket(
            "gh-token", "vc-token", "DEP-1", "demo", "alice/demo"
        )
    except backend_client.BackendUnavailable:
        return
    raise AssertionError("BackendUnavailable expected")


# --- 15. Phase 2 targets the DeployX-02 MultiUser webhooks -------------

def test_deployx02_n8n_defaults():
    import config

    assert config.DEPLOYX_N8N_WEBHOOK_URL == DEPLOYX_02_N8N_WEBHOOK
    assert (
        config.DEPLOYX_N8N_VERCEL_WEBHOOK_URL == DEPLOYX_02_VERCEL_WEBHOOK
    )


def test_deployx01_webhooks_are_not_used():
    """The legacy DeployX-01 paths must never be the built-in default."""
    import config

    assert config.DEPLOYX_N8N_WEBHOOK_URL != (
        "https://siva327.app.n8n.cloud/webhook/deployx")
    assert config.DEPLOYX_N8N_VERCEL_WEBHOOK_URL != (
        "https://siva327.app.n8n.cloud/webhook/deployx-vercel")


def test_backend_url_default_is_localhost():
    import config

    assert config.DEPLOYX_BACKEND_URL.startswith("http://127.0.0.1")
    assert config.DEPLOYX_BACKEND_URL.endswith(":8000")


def test_env_example_contains_only_placeholders():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / "backend" / ".env.example").read_text(encoding="utf-8")

    assert "DEPLOYX_BACKEND_URL=http://127.0.0.1:8000" in text
    assert "DEPLOYX_ENCRYPTION_KEY=" in text
    assert "DEPLOYX_N8N_SERVICE_KEY=" in text

    # No real secrets are committed.
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        _key, _sep, value = line.partition("=")
        assert value == "" or value.startswith("http://127.0.0.1"), line

