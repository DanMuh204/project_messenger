from uuid import UUID
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


class Verifier:
    def __init__(self, settings):
        self.settings = settings
        self.key = load_pem_public_key(settings.public_key.read_bytes())
        if not isinstance(self.key, RSAPublicKey) or self.key.key_size < 2048:
            raise ValueError("RSA public key required")

    def verify(self, token):
        claims = jwt.decode(
            token,
            self.key,
            algorithms=["RS256"],
            issuer=self.settings.issuer,
            audience=self.settings.audience,
            options={"require": ["sub", "iat", "exp", "iss", "aud"]},
        )
        if isinstance(claims["exp"], bool) or not isinstance(
            claims["exp"], (int, float)
        ):
            raise ValueError("Invalid expiry")
        return UUID(claims["sub"]), claims["exp"]
