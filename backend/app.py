"""
DeployX backend application factory.

Run locally with::

    python -m backend.app
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify

from backend import config, crypto
from backend.auth import error_response
from backend.db import init_db, reset_engine
from backend.routes import n8n as n8n_routes
from backend.routes import oauth as oauth_routes


def create_app() -> Flask:
    """Build the Flask application (used by ``flask run`` and by tests)."""
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    _install_safe_logging(app)

    init_db()

    app.register_blueprint(oauth_routes.bp)
    app.register_blueprint(n8n_routes.bp)

    @app.get("/health")
    def health():
        # Non-sensitive only: never echo key material.
        return jsonify(
            {
                "ok": True,
                "service": "deployx-backend",
                "configured": config.is_configured(),
            }
        )

    @app.get("/health/config")
    def health_config():
        """Diagnostic view of configuration. Contains no secrets."""
        return jsonify({"ok": True, "config": config.describe()})

    @app.errorhandler(404)
    def not_found(_error):
        return error_response("not_found", "Unknown endpoint.", 404)

    @app.errorhandler(405)
    def not_allowed(_error):
        return error_response("method_not_allowed", "Method not allowed.", 405)

    @app.errorhandler(Exception)
    def unhandled(error):
        # Never surface internals (they could contain provider details).
        app.logger.exception("Unhandled error: %s", type(error).__name__)
        return error_response(
            "internal_error", "The backend could not complete the request.", 500
        )

    return app


def _install_safe_logging(app: Flask) -> None:
    """
    Attach the redacting filter so an accidental log can never leak a token.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(crypto.redacting_filter)

    root = logging.getLogger("deployx.backend")
    if not any(
        getattr(existing, "_deployx_backend", False) for existing in root.handlers
    ):
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        handler._deployx_backend = True  # type: ignore[attr-defined]
    root.propagate = False

    app.logger.handlers = list(root.handlers)


def main() -> None:  # pragma: no cover - manual run
    app = create_app()

    if not config.is_configured():
        print("")
        print("DeployX backend is not fully configured.")
        print("Add DEPLOYX_ENCRYPTION_KEY and DEPLOYX_N8N_SERVICE_KEY")
        print("to backend/.env before using n8n endpoints.")
        print("")

    print(f"DeployX backend listening on http://{config.BIND_HOST}:{config.BIND_PORT}")
    app.run(host=config.BIND_HOST, port=config.BIND_PORT, debug=False)


if __name__ == "__main__":  # pragma: no cover
    main()
