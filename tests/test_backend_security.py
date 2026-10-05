"""Backend security tests: startup, crypto, service key, tickets, logging."""

from __future__ import annotations

import logging
from datetime import timedelta

from conftest import (
    ALICE_GITHUB_TOKEN,
    ALICE_VERCEL_TOKEN,
    SERVICE_HEADERS,
    SERVICE_KEY,
    issue_ticket,
)


# --- 1. backend starts ---------------------------------------------------

def test_backend_starts(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["service"] == "deployx-backend"


# --- 2. encryption / decryption ----------------------------------------

def test_encrypt_decrypt_roundtrip():
    from backend import crypto

    secret = "ghp_a_provider_token"
    ciphertext = crypto.encrypt_secret(secret)

    assert ciphertext != secret
    assert secret not in ciphertext
    assert crypto.decrypt_secret(ciphertext) == secret


def test_decrypt_with_wrong_key_fails():
    import importlib

    from cryptography.fernet import Fernet

    from backend import crypto

    ciphertext = crypto.encrypt_secret("secret")
    original = crypto._fernet
    crypto._fernet = lambda: Fernet(Fernet.generate_key())
    try:
        try:
            crypto.decrypt_secret(ciphertext)
        except crypto.DecryptionError:
            return
        raise AssertionError("DecryptionError expected")
    finally:
        crypto._fernet = original
        importlib.reload(crypto)


def test_stored_token_is_encrypted(client, alice):
    from backend import crypto
    from backend.db import session_scope
    from backend.models import ProviderConnection

    with session_scope() as session:
        rows = session.query(ProviderConnection).all()
        assert rows, "provider connections should exist"
        for row in rows:
            assert row.encrypted_access_token not in (
                ALICE_GITHUB_TOKEN,
                ALICE_VERCEL_TOKEN,
            )
            assert crypto.decrypt_secret(row.encrypted_access_token) in (
                ALICE_GITHUB_TOKEN,
                ALICE_VERCEL_TOKEN,
            )


# --- 3. provider tokens are never returned ------------------------------

def test_tokens_never_returned_to_cli(client, alice):
    session_text = str(alice["session_response"])
    assert ALICE_GITHUB_TOKEN not in session_text
    assert ALICE_VERCEL_TOKEN not in session_text

    ticket_response = issue_ticket(client, alice["session_token"])
    assert ticket_response.status_code == 200
    ticket_text = ticket_response.get_data(as_text=True)
    assert ALICE_GITHUB_TOKEN not in ticket_text
    assert ALICE_VERCEL_TOKEN not in ticket_text


def test_tokens_never_returned_to_n8n(client, alice, github):
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    context = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers=SERVICE_HEADERS,
    )
    assert context.status_code == 200
    assert ALICE_GITHUB_TOKEN not in context.get_data(as_text=True)
    assert ALICE_VERCEL_TOKEN not in context.get_data(as_text=True)

    created = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    assert created.status_code == 200
    assert ALICE_GITHUB_TOKEN not in created.get_data(as_text=True)
    assert ALICE_VERCEL_TOKEN not in created.get_data(as_text=True)


def test_only_ticket_hash_is_stored(client, alice):
    from backend import crypto
    from backend.db import session_scope
    from backend.models import DeploymentTicket

    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    with session_scope() as session:
        row = session.query(DeploymentTicket).one()
        assert row.ticket_hash == crypto.hash_token(ticket)
        assert ticket not in (row.ticket_hash,)


# --- 4/5. n8n service key ----------------------------------------------

def test_n8n_missing_service_key_rejected(client, alice):
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]
    response = client.post(
        "/v1/n8n/deployment-context", json={"ticket": ticket}
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_n8n_invalid_service_key_rejected(client, alice):
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]
    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_n8n_valid_service_key_works(client, alice):
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]
    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200
    context = response.get_json()["context"]
    assert context["github_username"] == "alice"
    assert context["user_id"] == alice["user"]["user_id"]


# --- 6. ticket creation -------------------------------------------------

def test_ticket_requires_session(client):
    response = client.post("/v1/deployment-ticket", json={})
    assert response.status_code == 401


def test_ticket_created_with_context(client, alice):
    response = issue_ticket(client, alice["session_token"])
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload["ticket"], str) and len(payload["ticket"]) > 20
    assert payload["context"]["repository"] == "alice/demo"
    assert payload["expires_at"]


# --- 7. expired ticket --------------------------------------------------

def test_expired_ticket_rejected(client, alice):
    from backend import crypto, db
    from backend.models import DeploymentTicket, utcnow

    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    with db.session_scope() as session:
        row = (
            session.query(DeploymentTicket)
            .filter_by(ticket_hash=crypto.hash_token(ticket))
            .one()
        )
        row.expires_at = utcnow() - timedelta(minutes=1)

    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "ticket_expired"


def test_unknown_ticket_rejected(client):
    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": "not-a-real-ticket"},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "invalid_ticket"


# --- 8. used ticket -----------------------------------------------------

def test_ticket_survives_repository_stage(client, alice, github):
    """Strategy C: creating the repo must NOT close the ticket."""
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    created = client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    assert created.status_code == 200

    reused = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers=SERVICE_HEADERS,
    )
    assert reused.status_code == 200


def test_ticket_stays_open_while_building(client, alice, github, vercel):
    vercel.ready_state = "BUILDING"
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    deploy = client.post(
        "/v1/n8n/vercel/deployments/create",
        json={
            "ticket": ticket,
            "name": "demo",
            "git_source": {"org": "alice", "repo": "demo", "ref": "main"},
        },
        headers=SERVICE_HEADERS,
    )
    assert deploy.status_code == 200
    assert deploy.get_json()["deployment"]["project_id"] == "prj_test_1"

    status = client.post(
        "/v1/n8n/vercel/deployments/status",
        json={"ticket": ticket, "deployment_id": "dpl_1"},
        headers=SERVICE_HEADERS,
    )
    assert status.status_code == 200
    assert status.get_json()["ticket_closed"] is False

    # Still usable for the next poll.
    again = client.post(
        "/v1/n8n/vercel/deployments/status",
        json={"ticket": ticket, "deployment_id": "dpl_1"},
        headers=SERVICE_HEADERS,
    )
    assert again.status_code == 200


def test_used_ticket_rejected_after_terminal_state(
    client, alice, github, vercel
):
    """Strategy C: a terminal build state closes the ticket for good."""
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    client.post(
        "/v1/n8n/github/repos/create",
        json={"ticket": ticket, "name": "demo"},
        headers=SERVICE_HEADERS,
    )
    client.post(
        "/v1/n8n/vercel/deployments/create",
        json={
            "ticket": ticket,
            "name": "demo",
            "git_source": {"org": "alice", "repo": "demo", "ref": "main"},
        },
        headers=SERVICE_HEADERS,
    )

    vercel.ready_state = "READY"
    status = client.post(
        "/v1/n8n/vercel/deployments/status",
        json={"ticket": ticket, "deployment_id": "dpl_1"},
        headers=SERVICE_HEADERS,
    )
    assert status.status_code == 200
    assert status.get_json()["ticket_closed"] is True
    assert status.get_json()["deployment"]["project_id"] == "prj_test_1"

    # Replay on any endpoint is now rejected.
    for path, body in (
        ("/v1/n8n/deployment-context", {"ticket": ticket}),
        ("/v1/n8n/github/repos/get", {"ticket": ticket, "repository": "a/b"}),
    ):
        replay = client.post(path, json=body, headers=SERVICE_HEADERS)
        assert replay.status_code == 401
        assert replay.get_json()["error"] == "ticket_used"


def test_service_key_header_accepted(client, alice):
    """DeployX-02 sends X-DeployX-Service-Key instead of a Bearer token."""
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]

    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers={"X-DeployX-Service-Key": SERVICE_KEY},
    )
    assert response.status_code == 200


def test_service_key_bearer_still_accepted(client, alice):
    """Backwards compatible: Authorization: Bearer keeps working."""
    ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]
    response = client.post(
        "/v1/n8n/deployment-context",
        json={"ticket": ticket},
        headers=SERVICE_HEADERS,
    )
    assert response.status_code == 200


# --- 13. no credentials in logs ----------------------------------------

def test_no_credentials_in_logs(client, alice, github):
    messages = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    logger = logging.getLogger("deployx.backend")
    handler = ListHandler()
    logger.addHandler(handler)
    try:
        ticket = issue_ticket(client, alice["session_token"]).get_json()["ticket"]
        client.post(
            "/v1/n8n/deployment-context",
            json={"ticket": ticket},
            headers=SERVICE_HEADERS,
        )
        client.post(
            "/v1/n8n/github/repos/create",
            json={"ticket": "bad-ticket", "name": "x"},
            headers=SERVICE_HEADERS,
        )
        github.fail_with = 500
        failing_ticket = issue_ticket(
            client, alice["session_token"]
        ).get_json()["ticket"]
        client.post(
            "/v1/n8n/github/repos/create",
            json={"ticket": failing_ticket, "name": "y"},
            headers=SERVICE_HEADERS,
        )
    finally:
        logger.removeHandler(handler)

    joined = "\n".join(messages)
    assert ALICE_GITHUB_TOKEN not in joined
    assert ALICE_VERCEL_TOKEN not in joined
    assert SERVICE_HEADERS["Authorization"].split()[-1] not in joined
