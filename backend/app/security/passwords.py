"""One-way, salted password hashing (task 3.1).

This module implements credential hashing for Requirement 1.7 ("store user
credentials using a one-way salted hashing algorithm") and underpins design
Property 1 (hashing is one-way and salted). It exposes two functions:

- :func:`hash_password` -- turn a plaintext password into a bcrypt hash string.
- :func:`verify_password` -- check a plaintext password against a stored hash.

Why bcrypt directly (not passlib)
---------------------------------
The design names "bcrypt (passlib)", but in this environment ``passlib`` 1.7.4
is incompatible with ``bcrypt`` 5.x: passlib's backend-probing code reads
``bcrypt.__about__.__version__`` (removed in bcrypt 4+) and, worse, its startup
"wrap bug" detection hashes a 72-byte test value -- which bcrypt 5.x now rejects
with ``ValueError`` instead of silently truncating. The net effect is that even
``CryptContext(schemes=["bcrypt"]).hash("hello")`` raises. Rather than pin an
older bcrypt, we call the well-maintained ``bcrypt`` library directly. The
result is identical in substance to the design's intent: a salted, one-way
bcrypt hash with the salt embedded in the ``$2b$...`` hash string, verifiable in
(near) constant time. No DB or settings coupling -- this module is standalone
and importable by the bootstrap (3.3) and login (3.4) tasks.

The 72-byte input limit
-----------------------
bcrypt only considers the first 72 bytes of the (UTF-8 encoded) password and
bcrypt 5.x raises if handed more. We therefore truncate the encoded password to
72 bytes in BOTH hashing and verification, so the two stay consistent and a
correct password always verifies. Documented behavior: only the first 72 UTF-8
bytes of a password are significant (standard bcrypt). Login validation caps
credential length at 256 characters in a later task; truncation here is purely
defensive so this module never raises on a long input.
"""

from __future__ import annotations

import bcrypt

__all__ = ["hash_password", "verify_password"]

# bcrypt operates on at most 72 bytes of input; longer inputs are truncated so
# we never trip bcrypt 5.x's "password cannot be longer than 72 bytes" error.
_BCRYPT_MAX_BYTES = 72


def _encode(plaintext: str) -> bytes:
    """UTF-8 encode and truncate a password to bcrypt's 72-byte input limit.

    Truncation is applied to the encoded bytes (bcrypt is byte-oriented). The
    same truncation runs for hashing and verification so a correct password
    always matches its stored hash.
    """
    return plaintext.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plaintext: str) -> str:
    """Hash ``plaintext`` with bcrypt and return the hash string.

    A fresh random salt is generated per call (via ``bcrypt.gensalt``) and is
    embedded in the returned ``$2b$...`` string, so two calls with the same
    password return different hashes. The result is one-way: the plaintext
    cannot be recovered from it.
    """
    hashed = bcrypt.hashpw(_encode(plaintext), bcrypt.gensalt())
    return hashed.decode("ascii")


def verify_password(plaintext: str, password_hash: str) -> bool:
    """Return ``True`` iff ``plaintext`` matches ``password_hash``.

    Uses bcrypt's constant-time comparison. Never raises for a wrong password
    or a malformed/garbage hash: any error (bad salt, non-ASCII hash, wrong
    type) is treated as a non-match and yields ``False``.
    """
    try:
        return bcrypt.checkpw(_encode(plaintext), password_hash.encode("ascii"))
    except (ValueError, TypeError, AttributeError, UnicodeEncodeError):
        # Malformed hash (e.g. not a bcrypt string), or a non-string input.
        return False
