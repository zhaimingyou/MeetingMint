"""Security primitives (task 3).

Currently exposes password hashing/verification (:mod:`app.security.passwords`).
Later auth tasks (sessions, login/logout, the auth dependency) build on top of
these primitives.
"""

from app.security.passwords import hash_password, verify_password

__all__ = ["hash_password", "verify_password"]
