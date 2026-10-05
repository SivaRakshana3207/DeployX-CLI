"""
DeployX backend — a local token broker between the DeployX CLI and the
DeployX-02 n8n workflow.

Design goals (multi-user, secure by default):

* The CLI authenticates once against GitHub and Vercel and hands the
  provider tokens to *this* backend only.
* Provider tokens are encrypted (Fernet / authenticated encryption) and
  never leave the backend again: not to n8n, not to the CLI.
* n8n receives a short-lived, single-use deployment ticket. The backend
  resolves the DeployX user from that ticket, so identity fields sent by
  n8n are never trusted for authorization.

This package is completely additive: DeployX-01 (the existing CLI and its
built-in n8n webhook defaults) keeps working unchanged.
"""
