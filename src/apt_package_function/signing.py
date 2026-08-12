# Copyright (c) Alianza, Inc. All rights reserved.
# Licensed under the MIT License.
"""GPG key management for signing the Debian repository.

Used by create-resources to obtain an armored private key (either loaded from a
file or freshly generated) and to derive the public key. The private key is
stored in Key Vault; the public key is published alongside the repository so
clients can verify it. The actual Release signing happens in the function
runtime (function_app.py), which is deployed standalone and cannot import this
package.
"""

import logging

import pgpy
from pgpy.constants import (
    CompressionAlgorithm,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


def load_private_key(path: str) -> str:
    """Load and validate an ASCII-armored private key from a file.

    Returns the armored private key. Raises ValueError if the file does not
    contain a usable, unlocked private key.
    """
    with open(path, encoding="utf-8") as f:
        blob = f.read()

    try:
        key, _ = pgpy.PGPKey.from_blob(blob)
    except Exception as e:  # pgpy raises a variety of errors
        raise ValueError(f"Could not parse a PGP key from {path}: {e}") from e

    if not key.is_public and key.is_protected:
        # We cannot sign non-interactively with a passphrase-protected key.
        raise ValueError(
            f"The private key in {path} is passphrase-protected; export an "
            "unprotected key for automated signing."
        )
    if key.is_public:
        raise ValueError(f"{path} contains a public key, not a private key.")

    log.info("Loaded private key %s from %s", key.fingerprint, path)
    return str(key)


def generate_private_key(uid_name: str) -> str:
    """Generate a new signing-capable private key, returned ASCII-armored."""
    log.info("Generating new signing key for %s", uid_name)
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 4096)
    uid = pgpy.PGPUID.new(uid_name)
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.ZLIB],
    )
    log.info("Generated key %s", key.fingerprint)
    return str(key)


def public_key_from_private(armored_private: str) -> str:
    """Derive the ASCII-armored public key from an armored private key."""
    key, _ = pgpy.PGPKey.from_blob(armored_private)
    return str(key.pubkey)


def _demo() -> None:
    """Self-check: generate, derive public, round-trip, reject bad input."""
    priv = generate_private_key("apt-package-function demo")
    assert "BEGIN PGP PRIVATE KEY BLOCK" in priv  # noqa: S101

    pub = public_key_from_private(priv)
    assert "BEGIN PGP PUBLIC KEY BLOCK" in pub  # noqa: S101

    # A generated key must actually be able to sign (verifies the usage flags).
    key, _ = pgpy.PGPKey.from_blob(priv)
    sig = key.sign("hello")
    pubkey, _ = pgpy.PGPKey.from_blob(pub)
    assert bool(pubkey.verify("hello", sig))  # noqa: S101

    # load_private_key must reject a public key.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".asc", delete=False) as f:
        f.write(pub)
        pub_path = f.name
    try:
        load_private_key(pub_path)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("load_private_key accepted a public key")

    print("signing self-check OK")


if __name__ == "__main__":
    _demo()
