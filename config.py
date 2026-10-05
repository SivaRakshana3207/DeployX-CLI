from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "deployx.db"

DATA_DIR.mkdir(exist_ok=True)

FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# =========================================================
# DeployX backend integrations (built in — no user setup)
# =========================================================
#
# These are the DeployX-02 MultiUser n8n webhook URLs, configured inside
# DeployX so users can run `deployx deploy` without setting any environment
# variables.
#
# An environment variable of the same name can still override the built-in
# default when needed (for example to point at a self-hosted instance or a
# test-webhook during development).
#
# Provider tokens are NEVER sent to n8n. The CLI hands them to the DeployX
# backend once and forwards only a short-lived deployment ticket.

DEPLOYX_N8N_WEBHOOK_URL = os.getenv(
    "DEPLOYX_N8N_WEBHOOK_URL",
    "https://siva327.app.n8n.cloud/webhook/deployx-mu",
).strip()

DEPLOYX_N8N_VERCEL_WEBHOOK_URL = os.getenv(
    "DEPLOYX_N8N_VERCEL_WEBHOOK_URL",
    "https://siva327.app.n8n.cloud/webhook/deployx-mu-vercel",
).strip()


# =========================================================
# Vercel OAuth (Sign in with Vercel)
# =========================================================
#
# DeployX authenticates each user with Vercel using the OAuth 2.0
# Device Authorization Grant (RFC 8628) — the same browser-based flow
# used by the official Vercel CLI. Users never paste a token.
#
# These endpoints are public (advertised at
# https://vercel.com/.well-known/openid-configuration) and contain no
# client secret, so nothing sensitive ships with DeployX.
#
# DeployX defaults to the public Vercel CLI client id. Register your
# own public client and override DEPLOYX_VERCEL_CLIENT_ID in production.

VERCEL_ISSUER = "https://vercel.com"

VERCEL_DEVICE_AUTHORIZATION_ENDPOINT = (
    "https://api.vercel.com/login/oauth/device-authorization"
)
VERCEL_TOKEN_ENDPOINT = "https://api.vercel.com/login/oauth/token"
VERCEL_REVOCATION_ENDPOINT = (
    "https://api.vercel.com/login/oauth/token/revoke"
)
VERCEL_USERINFO_ENDPOINT = "https://api.vercel.com/login/oauth/userinfo"

VERCEL_SCOPES = os.getenv(
    "DEPLOYX_VERCEL_SCOPES",
    "openid email profile offline_access",
)

DEPLOYX_VERCEL_CLIENT_ID = os.getenv(
    "DEPLOYX_VERCEL_CLIENT_ID",
    "cl_HYyOPBNtFMfHhaUn9L4QPfTZz6TP47bp",
)


# =========================================================
# DeployX backend (token broker)
# =========================================================
#
# DeployX-02 hands the user's GitHub/Vercel tokens to this backend exactly
# once and receives a short-lived deployment ticket, so the n8n workflow
# never receives a provider token.
#
# The backend is REQUIRED in Phase 2. If it cannot be reached, or rejects
# the credentials, the CLI stops: it never silently downgrades to the old
# DeployX-01 flow.
#
# DEPLOYX_BACKEND_URL must be reachable both by this CLI and by the
# DeployX-02 workflow. On a developer machine that is usually
# http://127.0.0.1:8000; if n8n Cloud calls the backend it must instead be
# the backend's public HTTPS address (for example a tunnel URL), configured
# through this variable or the environment.

DEPLOYX_BACKEND_URL = os.getenv(
    "DEPLOYX_BACKEND_URL",
    "http://127.0.0.1:8000",
).strip().rstrip("/")

DEPLOYX_BACKEND_TIMEOUT = int(
    os.getenv("DEPLOYX_BACKEND_TIMEOUT", "15")
)

