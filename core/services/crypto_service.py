"""
Encryption of secrets stored for a member (KoboToolbox personal token).

Fernet (AES-128-CBC + HMAC-SHA256, random IV, timestamp). The key never
leaves the server: KOBO_ENCRYPTION_KEY from .env (urlsafe base64, 32 bytes,
e.g. `python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`), or, when it is not set, a key
derived (HKDF-SHA256) from SUPABASE_SERVICE_ROLE_KEY - a server-only secret
already required. Changing that secret makes stored tokens unreadable: the
members simply enter them again.
"""

import base64
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core import config
from core.services.errors import ServiceError


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    explicit = os.environ.get("KOBO_ENCRYPTION_KEY")
    if explicit:
        return Fernet(explicit.encode())
    secret = config.SUPABASE_SERVICE_ROLE_KEY
    if not secret:
        raise ServiceError("Le chiffrement des jetons n'est pas configuré sur le serveur.")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"smartsaha",
               info=b"kobo-token-v1").derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> Optional[str]:
    """The plain text, or None if it can't be read with the current key."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
