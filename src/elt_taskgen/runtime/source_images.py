"""Digest-pinned images used by the isolated source-service stack.

These references are execution inputs, not implementation details.  Keep them
in one dependency-light module so source startup and certification identity
cannot drift apart.
"""

from __future__ import annotations


SOURCE_SERVICE_IMAGES: dict[str, str] = {
    "elt-api": (
        "python:3.11-slim@sha256:"
        "d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0"
    ),
    "elt-files": (
        "python:3.11-slim@sha256:"
        "d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0"
    ),
    "elt-localstack": (
        "localstack/localstack:4.8.1@sha256:"
        "8c9756bddc44625cd6e890f5e4bcc29b045eebe108b268482b848f4f832ec153"
    ),
    "elt-mongodb": (
        "mongo:8.2@sha256:"
        "e0ce8c35124d4a9f9785532d1f268f39e9728ffa1cb38f46fa482436424c4bd3"
    ),
    "elt-postgres": (
        "postgres:16@sha256:"
        "f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
    ),
}


__all__ = ["SOURCE_SERVICE_IMAGES"]
