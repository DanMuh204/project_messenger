from typing import Annotated
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import AuthSettings
from .errors import AppError

bearer = HTTPBearer(auto_error=False)


class TokenVerifier:
    def __init__(self, settings: AuthSettings):
        self.settings = settings
        self.public_key = load_pem_public_key(settings.public_key_file.read_bytes())
        if not isinstance(self.public_key, RSAPublicKey) or self.public_key.key_size < 2048:
            raise ValueError("JWT_PUBLIC_KEY_FILE must contain an RSA public key")

    def verify(self, token: str) -> UUID:
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["sub", "iat", "exp", "iss", "aud"]},
            )
            return UUID(claims["sub"])
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            raise AppError(401, "unauthorized", "A valid Bearer token is required") from None


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UUID:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError(401, "unauthorized", "A valid Bearer token is required")
    return request.app.state.verifier.verify(credentials.credentials)
