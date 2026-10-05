"""Provider brokerage tests: ownership isolation and safe provider errors."""

from __future__ import annotations

from conftest import (
    ALICE_GITHUB_TOKEN,
    ALICE_VERCEL_TOKEN,
    SERVICE_HEADERS,
    issue_ticket,
)


def _ticket(client, alice, **overrides):
    response = issue_ticket(client, alice["session_token"], **overrides)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["ticket"]


# --- 9. Alice can access Alice's repository -----------------------------

def test_alice_can_access_own_repository(client, alice, github):
    ticket = _ticket(client, alice)

    created = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    assert created.status_code == 200
    repository = created.get_json()["repository"]
    assert repository["full_name"] == "alice/demo"
    assert repository["owner"] == "alice"
    assert repository["created"] is True

    # Reuse: creating the same repo again reports created=False.
    reused = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": _ticket(client, alice), "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    assert reused.status_code == 200
    assert reused.get_json()["repository"]["created"] is False

    fetched = client.post(
        "/v1/n8n/github/repos/get",
        json={"ticket": _ticket(client, alice), "repository": "alice/demo"},
        headers=SERVICE_HEADERS,
    )
    assert fetched.status_code == 200
    assert fetched.get_json()["repository"]["owner"] == "alice"


def test_backend_uses_stored_token_for_owner_calls(client, alice, github):
    ticket = _ticket(client, alice)
    client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )

    calls = [headers for _method, _url, headers in github.calls]
    assert calls, "the backend should call GitHub"
    for headers in calls:
        assert headers["Authorization"] == f"Bearer {ALICE_GITHUB_TOKEN}"


# --- 10. Alice cannot access Bob's repository ---------------------------

def test_alice_cannot_access_bob_repository(client, alice, github):
    response = client.post(
        "/v1/n8n/github/repos/get",
        json={"ticket": _ticket(client, alice), "repository": "bob/demo"},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 403
    body = response.get_json()
    assert body["error"] == "identity_mismatch"
    assert ALICE_GITHUB_TOKEN not in response.get_data(as_text=True)


def test_repo_create_ignores_requested_owner(client, alice, github):
    """A caller cannot push the repo into somebody else's account."""
    ticket = _ticket(client, alice)
    response = client.post(
        "/v1/n8n/github/repos/create",
        json={
            "ticket": ticket,
            "name": "demo",
            "owner": "bob",
            "github_username": "bob",
        },
        headers=SERVICE_HEADERS,
    )
    # The spoofed identity is rejected outright.
    assert response.status_code == 403
    assert response.get_json()["error"] == "identity_mismatch"

    # Even without the spoof, the owner always comes from the token.
    ticket = _ticket(client, alice)
    response = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo2"},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200
    assert response.get_json()["repository"]["owner"] == "alice"
    assert "bob/demo2" not in github.repos


# --- 11. Alice cannot deploy into Bob's Vercel account ------------------

def test_alice_cannot_deploy_to_bob_vercel_scope(client, alice, vercel):
    response = client.post(
        "/v1/n8n/vercel/deployments/create",
        json={
            "ticket": _ticket(client, alice),
            "name": "demo",
            "team_id": "team-bob",
            "git_source": {"org": "alice", "repo": "demo", "ref": "main"},
        },
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 403
    body = response.get_json()
    assert body["error"] == "identity_mismatch"
    assert ALICE_VERCEL_TOKEN not in response.get_data(as_text=True)

    # No Vercel deployment was created at all.
    assert [call for call in vercel.calls if "deployments" in call[1]] == []


def test_alice_deploys_into_own_vercel_scope(client, alice, github, vercel):
    # The `github` fixture is required: backend/vercel.py resolves the
    # numeric repository id through the GitHub API, so without it this
    # test would make a REAL network call with a fake token.
    ticket = _ticket(client, alice)

    # The repository must exist first: that is what the real CLI does
    # before deploying, and Vercel needs its id.
    created = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    assert created.status_code == 200

    response = client.post(
        "/v1/n8n/vercel/deployments/create",
        json={
            "ticket": _ticket(client, alice),
            "name": "demo",
            "git_source": {"org": "alice", "repo": "demo", "ref": "main"},
        },
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200
    assert response.get_json()["deployment"]["id"] == "dpl_1"

    deploy_calls = [call for call in vercel.calls if "deployments" in call[1]]
    assert deploy_calls
    assert deploy_calls[0][2]["Authorization"] == f"Bearer {ALICE_VERCEL_TOKEN}"


def test_env_names_only_never_values(client, alice, vercel):
    response = client.post(
        "/v1/n8n/vercel/env-names",
        json={"ticket": _ticket(client, alice), "project_id": "prj_1"},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    names = response.get_json()["env_var_names"]
    assert names == ["API_KEY", "DATABASE_URL"]
    assert "super-secret-value" not in text
    assert "postgres://secret" not in text


# --- 12. provider failures return safe errors ---------------------------

def test_github_failure_returns_safe_error(client, alice, github):
    github.fail_with = 500
    response = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": _ticket(client, alice), "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 502
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"] == "provider_error"
    text = response.get_data(as_text=True)
    assert ALICE_GITHUB_TOKEN not in text
    assert "boom" not in text


def test_vercel_failure_returns_safe_error(client, alice, vercel):
    vercel.fail_with = 500
    response = client.post(
        "/v1/n8n/vercel/deployments/status",
        json={"ticket": _ticket(client, alice), "deployment_id": "dpl_1"},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"] == "provider_error"
    assert ALICE_VERCEL_TOKEN not in response.get_data(as_text=True)


def test_provider_not_connected(client):
    """A ticket without a stored Vercel credential fails safely."""
    from services import github_service

    original = github_service.get_authenticated_user
    github_service.get_authenticated_user = lambda token: (
        {"id": 2, "login": "bob"} if token == ALICE_GITHUB_TOKEN else None
    )
    try:
        response = client.post(
            "/v1/session",
            json={"github_token": ALICE_GITHUB_TOKEN},
        )
        assert response.status_code == 200
        session_token = response.get_json()["session_token"]
    finally:
        github_service.get_authenticated_user = original

    result = client.post(
        "/v1/n8n/vercel/deployments/status",
        json={
            "ticket": _ticket(client, {"session_token": session_token}),
            "deployment_id": "dpl_1",
        },
        headers=SERVICE_HEADERS,
    )
    assert result.status_code == 409
    assert result.get_json()["error"] == "provider_not_connected"
