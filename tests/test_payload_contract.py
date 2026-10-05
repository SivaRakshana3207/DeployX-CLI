"""
Deployment payload contract: `git_push_confirmed` is canonical everywhere
and no provider token ever reaches n8n.
"""

from __future__ import annotations

import io
import json
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CANONICAL = "git_push_confirmed"
LEGACY = "push_confirmed"

REQUIRED_FIELDS = {
    "deployment_id",
    "project_name",
    "framework",
    "project_type",
    "github_username",
    "github_repository",
    "github_url",
    "branch",
    "commit_sha",
    "git_push_confirmed",
    "env_var_names",
}

WORKFLOW = "n8n/DeployX-02-MultiUser.json"


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _vercel_payload(monkeypatch, info, context):
    from cli import deployx

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Resp(
            json.dumps(
                {"success": True, "vercel_url": "https://x.vercel.app"}
            ).encode("utf-8")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    buf = io.StringIO()
    with redirect_stdout(buf):
        deployx.call_n8n_vercel(
            info, 1, "https://github.com/alice/MyProject", "abc123",
            context, retries=1
        )
    return captured["payload"], buf.getvalue()


def test_vercel_payload_uses_canonical_field(monkeypatch):
    info = {
        "project_name": "MyProject",
        "framework": "nextjs",
        "project_type": "nextjs",
        "env_variable_names": ["API_KEY"],
    }
    payload, _ = _vercel_payload(monkeypatch, info, None)

    assert CANONICAL in payload, "canonical git_push_confirmed missing"
    assert LEGACY not in payload, "legacy push_confirmed must not be sent"
    assert payload[CANONICAL] is True


def test_vercel_payload_has_all_required_fields(monkeypatch):
    info = {
        "project_name": "MyProject",
        "framework": "nextjs",
        "project_type": "nextjs",
        "env_variable_names": ["API_KEY"],
    }
    context = {
        "github_username": "alice-dev",
        "github_repository": "alice-dev/MyProject",
        "vercel_username": "alice@example.com",
        "deployment_ticket": "dxt_abc",
    }
    payload, output = _vercel_payload(monkeypatch, info, context)

    missing = REQUIRED_FIELDS - set(payload)
    assert not missing, f"missing payload fields: {sorted(missing)}"

    assert payload["project_name"] == "MyProject"
    assert payload["commit_sha"] == "abc123"
    assert payload["branch"] == "main"
    assert payload["github_username"] == "alice-dev"
    assert payload["github_repository"] == "alice-dev/MyProject"
    assert payload["github_url"] == "https://github.com/alice-dev/MyProject"
    assert payload["deployment_ticket"] == "dxt_abc"
    assert payload["env_var_names"] == ["API_KEY"]

    serialised = json.dumps(payload)
    assert "vc_TOK" not in serialised
    assert "access_token" not in payload
    assert "vc_TOK" not in output


def test_workflow_reads_canonical_field():
    data = json.loads((ROOT / WORKFLOW).read_text(encoding="utf-8"))
    gate = next(n for n in data["nodes"] if n["name"] == "Git Push Confirmed?")
    expr = gate["parameters"]["conditions"]["conditions"][0]["leftValue"]

    assert CANONICAL in expr, "workflow must read git_push_confirmed"
    assert LEGACY in expr, "legacy name tolerated only as a read fallback"


def test_no_legacy_field_emitted_anywhere():
    offenders = []
    for rel in ("cli/deployx.py", "cli/backend_client.py",
                "backend/routes/n8n.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            if f'"{LEGACY}"' in line:
                offenders.append(f"{rel}: {line.strip()[:70]}")
    assert not offenders, offenders


def test_no_developer_account_hardcoded():
    targets = [
        ROOT / "cli" / "deployx.py",
        ROOT / "cli" / "auth.py",
        ROOT / "backend" / "github.py",
        ROOT / "backend" / "vercel.py",
        ROOT / "backend" / "routes" / "n8n.py",
        ROOT / "backend" / "routes" / "oauth.py",
        ROOT / "config.py",
    ]
    offenders = [t.name for t in targets
                 if "SivaRakshana3207" in t.read_text(encoding="utf-8")]
    assert not offenders, offenders


def test_workflow_has_no_provider_credentials():
    data = json.loads((ROOT / WORKFLOW).read_text(encoding="utf-8"))
    cred_types = set()
    for node in data["nodes"]:
        for ctype in (node.get("credentials") or {}):
            cred_types.add(ctype)
    assert not (cred_types & {"githubApi", "vercelApi", "githubOAuth2Api"}), (
        cred_types
    )


def test_workflow_is_not_published():
    """DeployX-02 must stay inactive until a human publishes it."""
    data = json.loads((ROOT / WORKFLOW).read_text(encoding="utf-8"))
    assert data.get("active") is False


