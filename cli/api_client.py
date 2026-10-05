from __future__ import annotations

import requests


def create_deployment(api_url, payload):
    response = requests.post(
        f"{api_url.rstrip('/')}/api/deployments",
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()
