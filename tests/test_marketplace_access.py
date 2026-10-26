"""Access tokens must prove identity and app ownership before account lookup."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from marketplace_access import AccessDenied, verify_access_token


class AccessTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537,
                                                    key_size=2048)

    def token(self, **changes):
        now = datetime.now(timezone.utc)
        claims = {"iss": "https://team.cloudflareaccess.com",
                  "aud": ["scout-audience"], "sub": "access-user-id",
                  "email": "Buyer@Example.com", "type": "app",
                  "iat": now, "exp": now + timedelta(minutes=5)}
        claims.update(changes)
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def verify(self, token):
        key_client = SimpleNamespace(get_signing_key_from_jwt=lambda _token:
                                     SimpleNamespace(key=self.private_key.public_key()))
        with patch("marketplace_access._keys", return_value=key_client):
            return verify_access_token(token, "team.cloudflareaccess.com",
                                       "scout-audience")

    def test_verified_email_and_subject(self):
        self.assertEqual(self.verify(self.token()),
                         {"email": "buyer@example.com", "subject": "access-user-id"})

    def test_rejects_anonymous_wrong_app_expired_and_service_tokens(self):
        cases = (None,
                 self.token(aud=["other-app"]),
                 self.token(iss="https://other.cloudflareaccess.com"),
                 self.token(exp=datetime.now(timezone.utc) - timedelta(minutes=2)),
                 self.token(type="service", email=""),
                 self.token(email="invalid"))
        for token in cases:
            with self.subTest(token=bool(token)), self.assertRaises(AccessDenied):
                self.verify(token)
