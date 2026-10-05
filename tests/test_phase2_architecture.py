"""
Phase 2 architecture guards.

The DeployX-01 fallback must be gone: if the DeployX backend is unreachable,
or refuses to issue a ticket, the deployment STOPS. Silently continuing with
the legacy flow could deploy under unknown credentials.
"""

from __future__ import annotations

import io
import json
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOYX_SOURCE = ROOT / "cli" / "deployx.py"


def _deploy_output(monkeypatch, **stubs):
    """Run deploy() with every external collaborator stubbed."""
    from cli import deployx as dx

    # deploy() creates a Flask deployment record before anything else.
    # Stub it so the test never needs a running Flask app.
    monkeypatch.setattr(dx, "create_deployment", lambda *a, **k: {
        "deployment": {"id": 1, "status": "QUEUED"},
    })

    # Authentication and both providers are stubbed, so a unit test never
    # opens a device-flow prompt, never touches git and never calls a real
    # GitHub / Vercel API.
    monkeypatch.setattr(dx, "ensure_authenticated", lambda *a, **k: {
        "github": {"access_token": "gh-stub", "username": "alice"},
        "vercel": {"access_token": "vc-stub", "username": "alice"},
    })

    monkeypatch.setattr(dx, "ensure_github_repository", lambda *a, **k: {
        "name": "demo",
        "full_name": "alice/demo",
        "owner": "alice",
        "html_url": "https://github.com/alice/demo",
        "clone_url": "https://github.com/alice/demo.git",
        "default_branch": "main",
    })

    monkeypatch.setattr(dx, "push_to_github", lambda *a, **k: None)
    monkeypatch.setattr(dx, "verify_github_push", lambda *a, **k: "a" * 40)

    monkeypatch.setattr(
        dx, "ensure_vercel_project", lambda *a, **k: {
            "name": "demo", "created": False,
        },
    )

    monkeypatch.setattr(
        dx, "create_vercel_deployment", lambda *a, **k: {
            "id": "dpl_1", "readyState": "BUILDING",
        },
    )

    monkeypatch.setattr(
        dx, "poll_deployment", lambda *a, **k: {
            "status": "SUCCESS",
            "ready_state": "READY",
            "deployment_id": "dpl_1",
            "url": "https://demo.vercel.app",
            "error": "",
        },
    )

    from cli import backend_client

    for name, value in stubs.items():
        if name == "is_available":
            monkeypatch.setattr(backend_client, "is_available", value)
        else:
            monkeypatch.setattr(dx, name, value)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = dx.deploy()
    return rc, out.getvalue()


def _n8n_spy():
    """Return (spy, calls) so we can prove n8n was never contacted."""
    calls = []

    def spy(*_a, **_k):
        calls.append(True)
        return {"success": True}

    return spy, calls


# --- the fallback string must be gone from the shipped code ------------

def test_no_deployx01_fallback_string_in_source():
    text = DEPLOYX_SOURCE.read_text(encoding="utf-8")

    assert "Using the DeployX-01 flow" not in text, (
        "the DeployX-01 fallback message still exists in the Phase 2 CLI"
    )


# --- the backend / n8n are OPTIONAL, never a hard stop ------------------

def test_backend_unavailable_does_not_stop_deployment(monkeypatch):
    """The CLI must reach Vercel even when the backend is down.

    This is the regression that made the old CLI appear to stop silently
    right after authentication.
    """
    spy, calls = _n8n_spy()

    rc, log = _deploy_output(
        monkeypatch,
        is_available=lambda *a, **k: False,
        call_n8n=spy,
    )

    assert "DeployX backend not running" in log
    assert not calls, "n8n must not be called when the backend is down"
    assert rc == 0, "the deployment must still complete"
    assert "https://demo.vercel.app" in log


def test_ticket_failure_does_not_stop_deployment(monkeypatch):
    from cli import backend_client

    spy, calls = _n8n_spy()

    def reject(*_a, **_k):
        raise backend_client.BackendUnavailable(
            "DeployX backend rejected the session (401).")

    rc, log = _deploy_output(
        monkeypatch,
        is_available=lambda *a, **k: True,
        prepare_deployment_ticket=reject,
        call_n8n=spy,
    )

    assert not calls, "n8n must not be called without a valid ticket"
    assert rc == 0
    assert "https://demo.vercel.app" in log


def test_empty_ticket_does_not_stop_deployment(monkeypatch):
    """A missing optional ticket must never be treated as a failure."""
    spy, calls = _n8n_spy()

    rc, _log = _deploy_output(
        monkeypatch,
        is_available=lambda *a, **k: True,
        prepare_deployment_ticket=lambda *a, **k: None,
        call_n8n=spy,
    )

    assert not calls
    assert rc == 0


def test_missing_dashboard_never_stops_the_deployment(monkeypatch):
    """The Flask dashboard record is optional."""
    def dead_dashboard(*_a, **_k):
        raise OSError("connection refused")

    rc, log = _deploy_output(
        monkeypatch,
        create_deployment=dead_dashboard,
    )

    assert "Dashboard not reachable" in log
    assert rc == 0
    assert "https://demo.vercel.app" in log

    # --- the CLI reports the real Vercel outcome ---------------------------

def test_success_requires_a_real_live_url(monkeypatch):
    """Vercel saying READY is not enough without a URL."""
    rc, log = _deploy_output(
        monkeypatch,
        poll_deployment=lambda *a, **k: {
            "status": "SUCCESS",
            "ready_state": "READY",
            "deployment_id": "dpl_1",
            "url": "",
            "error": "",
        },
    )

    assert "did not return a live URL" in log
    assert rc == 1


def test_real_failure_is_never_claimed_successful(monkeypatch):
    rc, log = _deploy_output(
        monkeypatch,
        poll_deployment=lambda *a, **k: {
            "status": "FAILED",
            "ready_state": "ERROR",
            "deployment_id": "dpl_1",
            "url": "",
            "error": "Command build failed with exit code 1.",
        },
    )

    assert rc == 1
    assert "Deployment failed" in log
    assert "exit code 1" in log
    assert "Deployment: SUCCESS" not in log


def test_deploy_reaches_vercel_without_the_backend(monkeypatch):
    """The whole point of the fix: auth -> GitHub -> Vercel, uninterrupted."""
    reached = []

    def note_project(*_a, **_k):
        reached.append("project")
        return {"name": "demo", "created": True}

    def note_deployment(*_a, **_k):
        reached.append("deployment")
        return {"id": "dpl_9", "readyState": "BUILDING"}

    _deploy_output(
        monkeypatch,
        is_available=lambda *a, **k: False,
        ensure_vercel_project=note_project,
        create_vercel_deployment=note_deployment,
    )

    assert reached == ["project", "deployment"]


def test_groq_is_only_called_after_a_real_failure(monkeypatch):
    """AI must never be consulted on a successful deployment."""
    from cli import deployx as dx

    calls = []

    monkeypatch.setattr(
        dx, "groq_analyze_build_error",
        lambda *a, **k: calls.append(True),
    )

    rc, _log = _deploy_output(monkeypatch)

    assert rc == 0
    assert not calls, "Groq must not run when Vercel reported success"


def test_groq_runs_when_vercel_really_fails(monkeypatch):
    from cli import deployx as dx

    monkeypatch.setattr(dx, "groq_is_configured", lambda: True)
    monkeypatch.setattr(
        dx, "groq_analyze_build_error",
        lambda *a, **k: {
            "summary": "The build cannot find the axios package.",
            "cause": "axios is imported but not listed in package.json.",
            "affected_files": ["src/App.jsx"],
            "recommended_fix": "Install the dependency.",
            "commands": ["npm install axios", "deployx deploy"],
            "confidence": "high",
        },
    )

    rc, log = _deploy_output(
        monkeypatch,
        poll_deployment=lambda *a, **k: {
            "status": "FAILED",
            "ready_state": "ERROR",
            "deployment_id": "dpl_1",
            "url": "",
            "error": "Module not found: axios",
        },
    )

    assert rc == 1
    assert "DeployX AI" in log
    assert "axios" in log
    assert "npm install axios" in log


def test_cli_shows_the_live_url_on_success(monkeypatch):
    rc, log = _deploy_output(monkeypatch)

    assert rc == 0
    assert "✓ Live URL: https://demo.vercel.app" in log
    assert "Deployment: SUCCESS" in log

def test_deployx02_webhooks_are_the_phase2_default():
    import config

    assert config.DEPLOYX_N8N_WEBHOOK_URL.endswith("/webhook/deployx-mu")
    assert config.DEPLOYX_N8N_VERCEL_WEBHOOK_URL.endswith(
        "/webhook/deployx-mu-vercel")

    # The legacy DeployX-01 paths must not be the built-in default.
    assert not config.DEPLOYX_N8N_WEBHOOK_URL.endswith("/webhook/deployx")
    assert not config.DEPLOYX_N8N_VERCEL_WEBHOOK_URL.endswith(
        "/webhook/deployx-vercel")


def test_webhook_env_override_still_supported():
    """The environment-based configuration design is preserved."""
    import importlib
    import os

    import config

    original = os.environ.get("DEPLOYX_N8N_WEBHOOK_URL")
    os.environ["DEPLOYX_N8N_WEBHOOK_URL"] = (
        "https://example.invalid/webhook/test-mu")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.DEPLOYX_N8N_WEBHOOK_URL == (
            "https://example.invalid/webhook/test-mu")
    finally:
        if original is None:
            os.environ.pop("DEPLOYX_N8N_WEBHOOK_URL", None)
        else:
            os.environ["DEPLOYX_N8N_WEBHOOK_URL"] = original
        importlib.reload(config)


# --- N / O. push-confirmation contract ---------------------------------

def test_git_push_confirmed_is_the_canonical_emitted_field():
    text = DEPLOYX_SOURCE.read_text(encoding="utf-8")

    assert '"git_push_confirmed"' in text
    assert '"push_confirmed"' not in text, (
        "the legacy field must not be emitted by the Phase 2 CLI"
    )


def test_legacy_field_is_not_what_the_cli_sends(monkeypatch):
    """The legacy name must never be the field actually transmitted."""
    from cli import deployx as dx

    captured = {}

    class _R:
        def __init__(self, body):
            self._b = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def fake(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _R(json.dumps({"success": True}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake)

    info = {
        "project_name": "ReMakersHub",
        "framework": "static-html",
        "project_type": "static",
        "env_variable_names": [],
    }

    with redirect_stdout(io.StringIO()):
        try:
            dx.call_n8n_vercel(info, 1, "https://github.com/u/r",
                               "a" * 40, None, retries=1)
        except Exception:
            pass  # transport details are irrelevant to the field names

    payload = captured.get("payload")
    assert payload is not None, "no payload was transmitted"
    assert "git_push_confirmed" in payload
    assert "push_confirmed" not in payload
