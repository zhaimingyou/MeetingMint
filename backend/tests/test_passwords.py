"""Unit tests for app.security.passwords (task 3.1).

These are example-based tests covering the concrete behaviors required by
Requirement 1.7 / design Property 1: hashes are not plaintext, correct
passwords verify, wrong passwords do not, each hash uses a distinct salt,
malformed hashes verify to False without raising, and unicode works.

The Property 1 hypothesis property test is task 3.6 (not implemented here).
"""

from __future__ import annotations

import bcrypt

from app.security.passwords import hash_password, verify_password


def test_hash_is_not_plaintext():
    password = "correct horse battery staple"
    hashed = hash_password(password)
    assert hashed != password
    assert password not in hashed
    # bcrypt hashes carry the standard identifier/cost prefix.
    assert hashed.startswith("$2")


def test_verify_correct_password_is_true():
    password = "s3cret-pa55word"
    hashed = hash_password(password)
    assert verify_password(password, hashed) is True


def test_verify_wrong_password_is_false():
    hashed = hash_password("the-right-one")
    assert verify_password("the-wrong-one", hashed) is False


def test_empty_string_password_roundtrips():
    hashed = hash_password("")
    assert verify_password("", hashed) is True
    assert verify_password("not empty", hashed) is False


def test_two_hashes_of_same_password_differ():
    password = "same-password-twice"
    h1 = hash_password(password)
    h2 = hash_password(password)
    # Distinct random salts => distinct hash strings, yet both verify.
    assert h1 != h2
    assert verify_password(password, h1) is True
    assert verify_password(password, h2) is True


def test_verify_against_malformed_hash_returns_false_without_raising():
    for garbage in ["", "not-a-hash", "$2b$broken", "12345", "💥" * 5]:
        assert verify_password("anything", garbage) is False


def test_verify_handles_non_string_hash_gracefully():
    # Defensive: a None/bytes hash must not raise, just fail to verify.
    assert verify_password("anything", None) is False  # type: ignore[arg-type]


def test_unicode_password_roundtrips():
    password = "会议纪要-密码-🔐"
    hashed = hash_password(password)
    assert verify_password(password, hashed) is True
    assert verify_password("会议纪要-密码", hashed) is False


def test_long_password_does_not_raise_and_truncates_at_72_bytes():
    # bcrypt only considers the first 72 bytes; inputs over that must not raise
    # and must remain verifiable.
    base = "a" * 72
    hashed = hash_password(base + "EXTRA-IGNORED-SUFFIX")
    assert verify_password(base, hashed) is True
    # Differing only beyond byte 72 still verifies (documented truncation).
    assert verify_password(base + "different-suffix", hashed) is True
    # Differing within the first 72 bytes does not verify.
    assert verify_password("b" * 72, hashed) is False


def test_hash_is_a_valid_bcrypt_string():
    # Sanity check the output is a genuine bcrypt hash the library accepts.
    hashed = hash_password("verify-me")
    assert bcrypt.checkpw(b"verify-me", hashed.encode("ascii")) is True
