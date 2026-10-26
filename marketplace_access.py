"""Verify Cloudflare Access application tokens before trusting an account email."""

from __future__ import annotations

from functools import lru_cache

import jwt
from jwt import PyJWKClient

from marketplace_alerts import normalize_email


class AccessDenied(ValueError):
    """The request has no valid Cloudflare Access application identity."""


@lru_cache(maxsize=8)
def _keys(team_domain: str) -> PyJWKClient:
    return PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs",
                       cache_jwk_set=True, lifespan=300, timeout=5)


def verify_access_token(token: str | None, team_domain: str,
                        audience: str) -> dict[str, str]:
    """Return a verified email and subject, or reject the entire request."""
    if not team_domain or not audience or not token:
        raise AccessDenied("Cloudflare Access login is required")
    if not isinstance(token, str) or len(token) > 16_384:
        raise AccessDenied("Invalid Cloudflare Access token")
    issuer = f"https://{team_domain}"
    try:
        key = _keys(team_domain).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"],
                            audience=audience, issuer=issuer,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                            leeway=30)
        if claims.get("type") != "app":
            raise AccessDenied("Cloudflare Access application login is required")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AccessDenied("Cloudflare Access subject is missing")
        email = normalize_email(claims.get("email"))
        return {"email": email, "subject": subject}
    except (jwt.PyJWTError, ValueError, TypeError, OSError) as error:
        raise AccessDenied("Invalid or expired Cloudflare Access login") from error
