import base64
import json
import time
import unittest
from unittest.mock import Mock, patch

import app.resource_auth as auth


class ResourceIdentityTests(unittest.TestCase):
    def test_runtime_identity_is_audience_scoped_and_cached(self):
        jwt_payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 120}).encode())
        opaque_token = "header." + jwt_payload.decode().rstrip("=") + ".signature"
        response = Mock(status_code=200)
        response.json.return_value = {"value": opaque_token}
        with (
            patch.object(auth, "_cached_token", None),
            patch.object(auth, "_cached_expiry", 0),
            patch.dict("os.environ", {
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://runner.actions.githubusercontent.com/identity",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-credential"
            }),
            patch.object(auth.requests, "get", return_value=response) as get,
        ):
            self.assertEqual(auth.resource_bearer_token(), opaque_token)
            self.assertEqual(auth.resource_bearer_token(), opaque_token)
            get.assert_called_once()
            self.assertEqual(get.call_args.kwargs["params"], {"audience": auth.AUDIENCE})
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_identity_credential_is_never_sent_to_untrusted_host(self):
        with (
            patch.object(auth, "_cached_token", None),
            patch.dict("os.environ", {
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.org/identity",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-credential"
            }),
            patch.object(auth.requests, "get") as get,
        ):
            with self.assertRaisesRegex(RuntimeError, "identity is unavailable"):
                auth.resource_bearer_token()
            get.assert_not_called()
