"""Deployment ticket contract: canonical field names for CLI -> backend -> n8n."""

from __future__ import annotations

from conftest import (
    ALICE_GITHUB_TOKEN,
    BOB_GITHUB_TOKEN,
    SERVICE_HEADERS,
    issue_ticket,
)

# The CLI and the DeployX-02 workflow use these exact names.
CANONICAL_TICKET_FIELD = "deployment_ticket"


def _ticket(client, alice):
    response = issue_ticket(client, alice["session_token"])
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["ticket"]


def test_ticket_is_accepted_under_canonical_field(client, alice):
    """The CLI's `deployment_ticket` must authenticate (this was the 401)."""
    ticket = _ticket(client, alice)

    response = client.post(
        "/v1/n8n/deployment-context",
        json={
            CANONICAL_TICKET_FIELD: ticket,
            "deployment_id": "DEP-00001",
            "branch": "main",
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["ok"] is True


def test_legacy_ticket_field_still_works(client, alice):
    """Older saved workflows send `ticket`; it stays valid."""
    ticket = _ticket(client, alice)

    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket, "deployment_id": "DEP-00001"},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200, response.get_data(as_text=True)


def test_ticket_accepted_from_header(client, alice):
    ticket = _ticket(client, alice)

    response = client.post(
        "/v1/n8n/deployment-context",
        json={"deployment_id": "DEP-00001"},
        headers={**SERVICE_HEADERS,
                 "X-DeployX-Deployment-Ticket": ticket},
    )

    assert response.status_code == 200, response.get_data(as_text=True)


def test_missing_ticket_is_rejected(client):
    response = client.post(
        "/v1/n8n/deployment-context",
        json={"deployment_id": "DEP-00001"},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "invalid_ticket"


def test_fabricated_ticket_is_rejected(client):
    """A made-up ticket must never authenticate."""
    response = client.post(
        "/v1/n8n/deployment-context",
        json={CANONICAL_TICKET_FIELD: "dxt_test_alice"},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "invalid_ticket"


def test_ticket_requires_service_key(client, alice):
    """The ticket alone is not enough; the service key is still mandatory."""
    ticket = _ticket(client, alice)

    response = client.post(
        "/v1/n8n/deployment-context",
        json={CANONICAL_TICKET_FIELD: ticket},
    )

    assert response.status_code in (401, 403)


def test_blank_ticket_is_rejected(client, alice):
    """Whitespace must not be mistaken for a ticket."""
    response = client.post(
        "/v1/n8n/deployment-context",
        json={CANONICAL_TICKET_FIELD: "   "},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 401


def test_ticket_is_bound_to_its_own_user(client, alice, monkeypatch):
    """Alice's ticket must resolve to Alice, never to another user."""
    from services import github_service, vercel_service

    ticket = _ticket(client, alice)

    def fake_github_user(token):
        if token == ALICE_GITHUB_TOKEN:
            return {"id": 1, "login": "alice"}
        if token == BOB_GITHUB_TOKEN:
            return {"id": 2, "login": "bob"}
        raise RuntimeError("invalid github token")

    monkeypatch.setattr(github_service, "get_authenticated_user",
                        fake_github_user)
    monkeypatch.setattr(
        vercel_service,
        "get_authenticated_user",
        lambda t: None if t == BOB_GITHUB_TOKEN else {
            "username": "alice@example.com",
            "email": "alice@example.com",
            "sub": "u-alice",
        },
    )

    bob = client.post(
        "/v1/session",
        json={"github_token": BOB_GITHUB_TOKEN},
    )
    assert bob.status_code == 200

    # Alice's ticket, but claiming to be Bob -> rejected.
    response = client.post(
        "/v1/n8n/deployment-context",
        json={CANONICAL_TICKET_FIELD: ticket,
              "github_username": "bob"},
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "identity_mismatch"


def test_deployment_id_and_commit_sha_travel_with_the_ticket(client, alice):
    """A real ticket + real commit SHA resolves the user's context."""
    ticket = _ticket(client, alice)
    sha = "5cd2efa846b603295fb90972f8b6771e592a6730"

    response = client.post(
        "/v1/n8n/deployment-context",
        json={
            CANONICAL_TICKET_FIELD: ticket,
            "deployment_id": "DEP-00001",
            "branch": "main",
            "commit_sha": sha,
            "framework": "nextjs",
            "project_type": "nextjs",
            "env_var_names": ["API_KEY"],
        },
        headers=SERVICE_HEADERS,
    )

    assert response.status_code == 200
    context = response.get_json()["context"]
    assert context["commit_sha"] == sha
    assert context["deployment_id"] == "DEP-00001"
    assert context["github_username"] == "alice"
