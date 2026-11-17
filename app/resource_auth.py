import base64
import json
import os
import sys
import time
from urllib.parse import urlparse

import requests

AUDIENCE = "khalid-downloader-resource-api"
_cached_token = None
_cached_expiry = 0


def resource_bearer_token():
    global _cached_token, _cached_expiry
    if _cached_token and _cached_expiry > time.time() + 30:
        return _cached_token

    endpoint = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    credential = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    url = urlparse(endpoint)
    host = url.hostname or ""
    if (not credential or url.scheme != "https" or url.username or url.password
            or not host.endswith(".actions.githubusercontent.com")):
        print("Resource identity unavailable: Actions OIDC environment missing or invalid.", file=sys.stderr)
        raise RuntimeError("GitHub Actions resource identity is unavailable.")
    try:
        response = requests.get(
            endpoint,
            params={"audience": AUDIENCE},
            headers={"Authorization": "Bearer " + credential},
            timeout=(3, 5),
            allow_redirects=False,
        )
        if response.status_code != 200:
            print(f"Resource identity endpoint rejected the request (HTTP {response.status_code}).", file=sys.stderr)
            raise ValueError("Token request failed.")
        token = response.json()["value"]
        _, payload, _ = token.split(".")
        # The Worker verifies the signature. This only determines when to refresh the cache.
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expiry = int(claims["exp"])
        if expiry <= time.time():
            raise ValueError("Expired token.")
    except (AttributeError, KeyError, TypeError, ValueError, requests.RequestException):
        raise RuntimeError("GitHub Actions resource identity could not be issued.") from None
    _cached_token, _cached_expiry = token, expiry
    return token
