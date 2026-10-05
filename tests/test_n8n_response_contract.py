"""
n8n response-contract tests.

The workflow's real JavaScript is executed under Node (the same runtime n8n
uses), so these tests catch runtime errors such as undefined identifiers that
a JSON-only syntax check would miss.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n" / "DeployX-02-MultiUser.json"

NODE = pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="node is required to evaluate the workflow expressions",
)

RUNNER = r"""
const expr = process.env.DX_EXPR;
const nodes = JSON.parse(process.env.DX_NODES);

function makeItem(name, idx) {
  const item = {
    json: (idx === 'last')
      ? nodes[name][nodes[name].length - 1]
      : nodes[name][0],
    isExecuted: Object.prototype.hasOwnProperty.call(nodes, name),
  };
  item.first = () => item;
  item.last = () => item;
  return item;
}
global.$ = makeItem;
global.$prevNode = { name: process.env.DX_PREV || '' };
global.$json = JSON.parse(process.env.DX_JSON || '{}');
try {
  console.log(JSON.stringify({ ok: true, value: eval(expr) }));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: e.name + ': ' + e.message }));
}
"""


def _params(name):
    data = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    node = next(n for n in data["nodes"] if n["name"] == name)
    return node.get("parameters", {})


def run_node(name, field, nodes, json_ctx=None, prev=""):
    """Evaluate one workflow expression the way n8n evaluates it."""
    expr = _params(name)[field]
    assert expr.strip().startswith("={{"), expr[:40]

    env = dict(
        os.environ,
        DX_EXPR=expr.strip()[3:-2],
        DX_NODES=json.dumps(nodes),
        DX_PREV=prev,
        DX_JSON=json.dumps(json_ctx or {}),
    )
    proc = subprocess.run(["node", "-e", RUNNER], capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr[:300]
    return json.loads(proc.stdout.strip())


REQUEST_BODY = {
    "deployment_id": "DEP-00030",
    "project_name": "ReMakersHub",
    "framework": "static-html",
    "project_type": "static",
    "github_username": "SivaRakshana3207",
    "github_repository": "SivaRakshana3207/ReMakersHub",
    "github_url": "https://github.com/SivaRakshana3207/ReMakersHub",
    "branch": "main",
    "commit_sha": "e83358b39522e68bbfaf8a52b4b321dbd77cd587",
    "git_push_confirmed": True,
    "env_var_names": [],
}

SUCCESS_NODES = {
    "DeployX Deploy Request": [{"body": REQUEST_BODY}],
    "Resolve Deploy User": [{
        "context": {"github_username": "SivaRakshana3207",
                    "vercel_username": "user@example.com"}}],
    "Get Vercel Deployment Status": [{
        "deployment": {"id": "dpl_real_001", "ready_state": "READY",
                       "url": "remakershub.vercel.app"}}],
    "Get Vercel Env Var Names": [{"env_var_names": ["API_KEY"]}],
}


def _summary_nodes():
    return copy.deepcopy({
        "DeployX Deploy Request": [{"body": REQUEST_BODY}],
        "Resolve Deploy User": [{"context": {
            "github_username": "SivaRakshana3207"}}],
    })


# --- the success responder must not throw at runtime -------------------

@NODE
def test_success_responder_runs_without_error():
    """Regression: an undefined `envList` raised a ReferenceError."""
    result = run_node("Respond Deployment Success", "responseBody",
                      copy.deepcopy(SUCCESS_NODES))

    assert result["ok"], result.get("error")


@NODE
def test_success_responder_runs_when_env_vars_requested():
    """The env-var branch previously threw ReferenceError: envList."""
    nodes = copy.deepcopy(SUCCESS_NODES)
    nodes["DeployX Deploy Request"][0]["body"]["env_var_names"] = ["API_KEY"]

    result = run_node("Respond Deployment Success", "responseBody", nodes)

    assert result["ok"], result.get("error")


@NODE
def test_success_response_matches_the_expected_contract():
    result = run_node("Respond Deployment Success", "responseBody",
                      copy.deepcopy(SUCCESS_NODES))
    body = json.loads(result["value"])

    assert body["success"] is True
    assert body["deployment_id"] == "DEP-00030"
    assert body["project_name"] == "ReMakersHub"
    assert body["framework"] == "static-html"
    assert body["github_repository"] == "SivaRakshana3207/ReMakersHub"
    assert body["github_url"].startswith("https://github.com/")
    assert body["vercel_url"] == "https://remakershub.vercel.app"
    assert body["vercel_deployment_id"] == "dpl_real_001"


@NODE
def test_success_response_never_contains_undefined():
    """Regression: the settings URL used to render `//undefined/`."""
    result = run_node("Respond Deployment Success", "responseBody",
                      copy.deepcopy(SUCCESS_NODES))

    assert "undefined" not in result["value"], result["value"][:300]


@NODE
def test_success_response_contains_no_tokens():
    result = run_node("Respond Deployment Success", "responseBody",
                      copy.deepcopy(SUCCESS_NODES))
    raw = result["value"].lower()

    for marker in ("ghp_", "github_pat_", "vcx_", "access_token", "bearer "):
        assert marker not in raw


# --- failures must be classified honestly ------------------------------

@NODE
def test_real_ticket_failure_is_classified_as_authorization():
    """A genuine invalid_ticket IS an authorization failure."""
    result = run_node("Summarize Failure", "jsonOutput", _summary_nodes(),
                      json_ctx={"error": "invalid_ticket",
                                "message": "The deployment ticket is not "
                                           "valid."},
                      prev="Resolve Deploy User")
    out = json.loads(result["value"])

    assert out["failure_stage"] == "authorization"
    assert out["auth_required"] is True


@NODE
def test_network_failure_is_not_reported_as_authorization():
    """
    Regression: every 'Resolve Deploy User' failure used to be hard-coded to
    `authorization`, so an unreachable backend produced a false
    "token expired / revoked" message.
    """
    result = run_node("Summarize Failure", "jsonOutput", _summary_nodes(),
                      json_ctx={}, prev="Resolve Deploy User")
    out = json.loads(result["value"])

    assert out["failure_stage"] == "backend_unreachable"
    assert out["auth_required"] is False
    assert "authoriz" not in out["error"].lower()
    assert "revoked" not in out["error"].lower()


@NODE
def test_non_auth_backend_error_keeps_its_own_code():
    result = run_node("Summarize Failure", "jsonOutput", _summary_nodes(),
                      json_ctx={"error": "provider_error",
                                "message": "GitHub rejected the credential."},
                      prev="Resolve Deploy User")
    out = json.loads(result["value"])

    assert out["failure_stage"] == "backend_error"
    assert out["auth_required"] is False
    assert "provider_error" in out["error"]


@NODE
def test_failure_response_keeps_structured_fields():
    """The AI guidance must never replace the structured error fields."""
    text = _params("Respond Deployment Failure")["responseBody"]

    for field in ("failure_stage", "error", "ai_fix_guidance"):
        assert field in text


# --- the CLI must surface the real reason ------------------------------

def test_cli_failure_report_shows_real_reason_first(capsys):
    """A generic AI suggestion must not be presented as the diagnosis."""
    from cli.deployx import _report_n8n_failure

    _report_n8n_failure({
        "success": False,
        "failure_stage": "backend_unreachable",
        "auth_required": False,
        "error": "DeployX backend call failed: no response from the backend.",
        "ai_fix_guidance": "The token is missing, expired, or revoked.",
    })

    out = capsys.readouterr().out
    assert "no response from the backend" in out
    assert out.index("Reason:") < out.index("Suggested steps")


def test_cli_failure_report_marks_ai_text_as_advisory(capsys):
    from cli.deployx import _report_n8n_failure

    _report_n8n_failure({
        "success": False,
        "failure_stage": "backend_unreachable",
        "error": "real cause",
        "ai_fix_guidance": "some AI text",
    })

    out = capsys.readouterr().out
    assert "AI-generated" in out


def test_cli_requires_url_and_deployment_id():
    """success without a live URL is not a completed deployment."""
    import inspect

    from cli import deployx

    source = inspect.getsource(deployx.deploy)

    # A live URL is mandatory before SUCCESS is ever reported.
    assert "if not vercel_url:" in source

    # A deployment id is mandatory: without one DeployX raises rather
    # than pretending a deployment started.
    assert "Vercel did not return a deployment id." in source

    # And SUCCESS is only reached after the real status check.
    assert 'vercel_outcome.get("status") != "SUCCESS"' in source

