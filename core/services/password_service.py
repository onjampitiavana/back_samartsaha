"""Temporary passwords for accounts created by an administrator."""

import secrets
import string
from typing import Optional

LOWER = string.ascii_lowercase
UPPER = string.ascii_uppercase
DIGITS = string.digits
# Unambiguous symbols that survive e-mail clients and copy / paste.
SYMBOLS = "!@#$%&*?-_+="
LENGTH = 8


def generate_password(length: int = LENGTH) -> str:
    """`length` characters with at least one lower-case letter, upper-case
    letter, digit and symbol, from a cryptographically secure source."""
    if length < 4:
        raise ValueError("A password needs at least 4 characters.")
    rng = secrets.SystemRandom()
    chars = [rng.choice(LOWER), rng.choice(UPPER), rng.choice(DIGITS), rng.choice(SYMBOLS)]
    pool = LOWER + UPPER + DIGITS + SYMBOLS
    chars += [rng.choice(pool) for _ in range(length - len(chars))]
    rng.shuffle(chars)
    return "".join(chars)


def password_problem(password: str, length: int = LENGTH) -> Optional[str]:
    """Why a password chosen by an administrator is refused (in French), or
    None if it follows the same rule as the generated ones: at least
    `length` characters with a lower-case letter, an upper-case letter, a
    digit and a special character."""
    if len(password) < length:
        return f"Le mot de passe doit contenir au moins {length} caractères."
    if any(c.isspace() for c in password):
        return "Le mot de passe ne doit pas contenir d'espace."
    missing = [label for label, ok in (
        ("une minuscule", any(c.islower() for c in password)),
        ("une majuscule", any(c.isupper() for c in password)),
        ("un chiffre", any(c.isdigit() for c in password)),
        ("un caractère spécial", any(not c.isalnum() for c in password)),
    ) if not ok]
    if missing:
        return "Le mot de passe doit contenir " + ", ".join(missing) + "."
    return None
