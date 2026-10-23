"""Fail when a CA bundle still carries a root Mozilla has removed.

This mirrors taostat/gm ``scripts/check_ca_bundle.py``; keep the two in step.
The validator image runs this at build time against the OS bundle and the
venv's certifi bundle (see ``Dockerfile``). Roots are matched by
the SHA-256 fingerprint of their DER encoding, so a renamed or re-encoded copy
of a removed root is still caught. Stdlib only: it runs in the bare runtime
image before anything else is installed.

Usage: python check_ca_bundle.py BUNDLE [BUNDLE ...]
"""

from __future__ import annotations

import hashlib
import re
import ssl
import sys
from pathlib import Path

REMOVED_ROOTS = {
    "D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4": "AAA Certificate Services",
    "86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B": "emSign ECC Root CA - G3",
}

_PEM_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL
)


class BundleError(Exception):
    """A bundle is unreadable, empty, or carries a removed root."""


def fingerprints(pem: str) -> list[str]:
    """Return the uppercase hex SHA-256 fingerprint of every certificate in ``pem``.

    Raises:
        BundleError: a ``BEGIN CERTIFICATE`` marker has no parseable block,
            which would otherwise let a malformed entry escape the check.
    """
    blocks = _PEM_BLOCK.findall(pem)
    if len(blocks) != pem.count("-----BEGIN CERTIFICATE-----"):
        raise BundleError("unterminated or nested certificate block")
    found = []
    for block in blocks:
        try:
            der = ssl.PEM_cert_to_DER_cert(block)
            # PEM_cert_to_DER_cert only decodes base64; loading the DER makes
            # OpenSSL parse it as X.509, so non-certificate bytes are rejected.
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=der)
        except (ValueError, ssl.SSLError) as exc:
            raise BundleError(f"unparseable certificate: {exc}") from exc
        found.append(hashlib.sha256(der).hexdigest().upper())
    return found


def check_bundle(path: Path) -> int:
    """Check one bundle and return its certificate count.

    Raises:
        BundleError: the bundle is empty, malformed, or carries a removed root.
    """
    try:
        found = fingerprints(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise BundleError(f"{path}: cannot read bundle: {exc}") from exc
    except BundleError as exc:
        raise BundleError(f"{path}: {exc}") from exc
    if not found:
        raise BundleError(f"{path}: no certificates")
    present = sorted(REMOVED_ROOTS[fp] for fp in set(found) & REMOVED_ROOTS.keys())
    if present:
        raise BundleError(f"{path}: carries removed root(s): {', '.join(present)}")
    return len(found)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    for arg in argv:
        try:
            count = check_bundle(Path(arg))
        except BundleError as exc:
            print(f"check_ca_bundle: {exc}", file=sys.stderr)
            return 1
        print(f"check_ca_bundle: {arg}: {count} roots, none removed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
