import hashlib
import secrets
import time
from uuid import UUID, uuid4

import anyio
import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
    load_pem_public_key,
)
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastapi import HTTPException


def unauthorized():
    return HTTPException(
        401, "Invalid credentials or session", headers={"WWW-Authenticate": "Bearer"}
    )


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def new_refresh():
    return secrets.token_urlsafe(32)


class Security:
    def __init__(self, settings):
        self.settings = settings
        self.private = load_pem_private_key(
            settings.private_key.read_bytes(), password=None
        )
        self.public = load_pem_public_key(settings.public_key.read_bytes())
        if not isinstance(self.private, RSAPrivateKey) or not isinstance(
            self.public, RSAPublicKey
        ):
            raise ValueError("RSA keys required")
        if (
            self.private.key_size < 2048
            or self.private.public_key().public_numbers()
            != self.public.public_numbers()
        ):
            raise ValueError("RSA key pair is invalid")
        self.hasher = PasswordHasher(
            time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID
        )
        self.limiter = anyio.CapacityLimiter(4)
        self.dummy = self.hasher.hash(secrets.token_urlsafe(32))

    async def hash_password(self, password):
        return await anyio.to_thread.run_sync(
            self.hasher.hash, password, limiter=self.limiter
        )

    async def check_password(self, hashed, password):
        def verify():
            try:
                return self.hasher.verify(hashed or self.dummy, password)
            except (VerificationError, InvalidHashError):
                return False

        return await anyio.to_thread.run_sync(verify, limiter=self.limiter)

    def access(self, user_id, family_id):
        now = int(time.time())
        return jwt.encode(
            {
                "sub": str(user_id),
                "sid": str(family_id),
                "jti": str(uuid4()),
                "iat": now,
                "exp": now + self.settings.access_seconds,
                "iss": self.settings.issuer,
                "aud": self.settings.audience,
            },
            self.private,
            algorithm="RS256",
        )

    def verify(self, token):
        try:
            payload = jwt.decode(
                token,
                self.public,
                algorithms=["RS256"],
                issuer=self.settings.issuer,
                audience=self.settings.audience,
                options={"require": ["sub", "iat", "exp", "iss", "aud"]},
            )
            return UUID(payload["sub"])
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            raise unauthorized() from None
