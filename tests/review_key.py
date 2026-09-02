from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _ensure_test_reviewer_key() -> Path:
    path = Path(tempfile.gettempdir()) / "uq-test-reviewer-ed25519-private.pem"
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("00" * 32))
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


REVIEWER_PRIVATE_KEY = _ensure_test_reviewer_key()
