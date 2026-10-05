"""
Unit tests for the real Vercel + Groq integrations.

Every external API is mocked, so these tests need no GitHub / Vercel / Groq
credentials and make no network calls. They prove DeployX reports what the
APIs actually said, and never invents a success.
"""

from __future__ import annotations

import pytest

from services import groq_service, vercel_service


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Requests:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _stub(monkeypatch, *responses):
    fake = _Requests(*responses)
    monkeypatch.setattr(vercel_service, "requests", fake)
    return fake


# --- Vercel error handling ---------------------------------------------

@pytest.mark.parametrize(
    "status,code",
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
def test_vercel_http_errors_are_begunner_friendly(monkeypatch, status, code):
    _stub(monkeypatch, _Response(status, {"error": {"message": "nope"}}))

    with pytest.raises(vercel_service.VercelError) as exc:
        vercel_service.get_project("token", "demo")

    assert exc.value.code == code


def test_vercel_401_mentions_vercel_token(monkeypatch):
    _stub(monkeypatch, _Response(401, {}))

    with pytest.raises(vercel_service.VercelError) as exc:
        vercel_service.get_project("secret-token", "demo")

    assert "VERCEL_TOKEN" in exc.value.message
    assert "secret-token" not in exc.value.message


def test_missing_token_is_reported_not_crashed():
    with pytest.raises(vercel_service.VercelError) as exc:
        vercel_service.get_project("", "demo")

    assert exc.value.code == "not_configured"
    assert "VERCEL_TOKEN" in exc.value.message


def test_vercel_network_failure_is_handled(monkeypatch):
    import requests

    class _Boom:
        def request(self, *a, **k):
            raise requests.ConnectionError("no network")

    monkeypatch.setattr(vercel_service, "requests", _Boom())

    with pytest.raises(vercel_service.VercelError) as exc:
        vercel_service.get_project("token", "demo")

    assert exc.value.code == "network_error"


def test_vercel_timeout_is_handled(monkeypatch):
    import requests

    class _Slow:
        def request(self, *a, **k):
            raise requests.Timeout("too slow")

    monkeypatch.setattr(vercel_service, "requests", _Slow())

    with pytest.raises(vercel_service.VercelError) as exc:
        vercel_service.get_project("token", "demo")

    assert exc.value.code == "timeout"

def test_existing_project_is_reused_not_recreated(monkeypatch):
    fake = _stub(monkeypatch, _Response(200, {"id": "prj_1", "name": "demo"}))

    project = vercel_service.ensure_project("token", "demo")

    assert project["created"] is False
    assert len(fake.calls) == 1, "no POST when the project already exists"


def test_missing_project_is_created(monkeypatch):
    fake = _stub(
        monkeypatch,
        _Response(404, {"error": {"message": "not found"}}),
        _Response(200, {"id": "prj_1", "name": "demo"}),
    )

    project = vercel_service.ensure_project("token", "demo")

    assert project["created"] is True
    assert fake.calls[1][0] == "POST"


def test_redeploy_reuses_the_same_project_name(monkeypatch):
    """A second deploy must never produce demo-2 / demo-3."""
    fake = _stub(
        monkeypatch,
        _Response(200, {"id": "prj_1", "name": "demo"}),
        _Response(200, {"id": "prj_1", "name": "demo"}),
        _Response(200, {"id": "prj_1", "name": "demo"}),
    )

    for _ in range(3):
        vercel_service.ensure_project("token", "demo")

    requested = [url for _method, url, _k in fake.calls]

    assert all(url.endswith("/v9/projects/demo") for url in requested)
    assert not any("demo-2" in url for url in requested)


# --- deployment status parsing -----------------------------------------

@pytest.mark.parametrize(
    "ready_state,status",
    [
        ("READY", "SUCCESS"),
        ("ERROR", "FAILED"),
        ("CANCELED", "FAILED"),
        ("BUILDING", "BUILDING"),
        ("DEPLOYING", "BUILDING"),
        ("QUEUED", "PENDING"),
        ("", "PENDING"),
    ],
)
def test_ready_state_maps_to_deployx_status(ready_state, status):
    assert vercel_service.map_ready_state(ready_state) == status


def test_build_failure_is_reported_as_failed(monkeypatch):
    monkeypatch.setattr(
        vercel_service,
        "get_deployment",
        lambda *a, **k: {
            "readyState": "ERROR",
            "error": {"message": "Command build failed."},
        },
    )

    result = vercel_service.poll_deployment(
        "token", "dpl_1", interval=0, timeout=5,
    )

    assert result["status"] == "FAILED"
    assert result["url"] == ""
    assert "Command build failed." in result["error"]


def test_success_returns_the_real_url(monkeypatch):
    monkeypatch.setattr(
        vercel_service,
        "get_deployment",
        lambda *a, **k: {
            "readyState": "READY",
            "url": "dpl-abc.vercel.app",
            "alias": ["demo.vercel.app"],
        },
    )

    result = vercel_service.poll_deployment(
        "token", "dpl_1", interval=0, timeout=5,
    )

    assert result["status"] == "SUCCESS"
    assert result["url"] == "https://demo.vercel.app"


def test_public_url_prefers_the_production_alias():
    payload = {
        "url": "dpl-xyz.vercel.app",
        "alias": ["dpl-git-main-user.vercel.app", "demo-user.vercel.app"],
    }
    assert vercel_service._public_url(payload) == "https://demo-user.vercel.app"


# --- secrets are never uploaded ----------------------------------------

def test_env_and_keys_are_never_uploaded(tmp_path):
    (tmp_path / "app.jsx").write_text("hello", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hunter2", encoding="utf-8")
    (tmp_path / "server.pem").write_text("-----BEGIN-----", encoding="utf-8")

    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "big.js").write_text("x", encoding="utf-8")

    names = {
        item["file"]
        for item in vercel_service.collect_project_files(tmp_path)
    }
# --- Groq: redaction, parsing, and graceful failure --------------------

def test_groq_is_unconfigured_without_a_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert groq_service.is_configured() is False
    assert groq_service.analyze_build_error("demo", "react", "err") is None


def test_groq_redacts_secret_values():
    cleaned = groq_service.redact("failed with ghp_abcdefghij1234567890")

    assert "ghp_abcdefghij1234567890" not in cleaned
    assert groq_service.REDACTED in cleaned


def test_groq_redacts_env_assignments():
    cleaned = groq_service.redact("MY_API_KEY=supersecret\nPATH=/usr/bin")

    assert "supersecret" not in cleaned
    assert "PATH=/usr/bin" in cleaned


def test_groq_prompt_never_contains_raw_secrets():
    prompt = groq_service.build_prompt(
        project="demo",
        framework="react",
        error="boom ghp_abcdefghij1234567890",
        logs="TOKEN=deadbeef",
    )

    assert "ghp_abcdefghij1234567890" not in prompt
    assert "deadbeef" not in prompt


def test_groq_parses_structured_response():
    parsed = groq_service._parse_response(
        '{"summary": "Build failed", "cause": "Missing dependency",'
        ' "affected_files": ["src/App.jsx"],'
        ' "recommended_fix": "npm install axios",'
        ' "commands": ["npm install axios"], "confidence": "high"}'
    )

    assert parsed["summary"] == "Build failed"
    assert parsed["affected_files"] == ["src/App.jsx"]
    assert parsed["confidence"] == "high"


def test_groq_parses_a_fenced_code_block():
    parsed = groq_service._parse_response(
        '```json\n{"summary": "Fenced", "confidence": "medium"}\n```'
    )

    assert parsed["summary"] == "Fenced"
    assert parsed["confidence"] == "medium"


def test_groq_never_invents_a_confidence():
    parsed = groq_service._parse_response(
        '{"summary": "x", "confidence": "99"}',
    )

    assert parsed["confidence"] == "low"


def test_groq_handles_unstructured_prose(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "Went wrong."}}]}

    class _R:
        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_service, "requests", _R())

    result = groq_service.analyze_build_error("demo", "react", "boom")

    assert result is not None
    assert result["confidence"] == "low"


def test_groq_api_key_never_appears_in_errors(monkeypatch):
    class _Resp:
        status_code = 401

        def json(self):
            return {}

    class _R:
        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setenv("GROQ_API_KEY", "super-secret-key")
    monkeypatch.setattr(groq_service, "requests", _R())

    with pytest.raises(groq_service.GroqError) as exc:
        groq_service.analyze_build_error("demo", "react", "boom")

    assert "super-secret-key" not in exc.value.message
    assert "GROQ_API_KEY" in exc.value.message


def test_groq_network_failure_is_soft(monkeypatch):
    import requests

    class _Boom:
        def post(self, *a, **k):
            raise requests.ConnectionError("offline")

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_service, "requests", _Boom())

    with pytest.raises(groq_service.GroqError) as exc:
        groq_service.analyze_build_error("demo", "react", "boom")

    assert "Deployment result will still be shown" in exc.value.message

def test_deployment_requires_at_least_one_file(tmp_path):
    with pytest.raises(vercel_service.VercelError) as exc:
        vercel_service.create_deployment("token", "demo", tmp_path)

    assert exc.value.code == "empty_project"
