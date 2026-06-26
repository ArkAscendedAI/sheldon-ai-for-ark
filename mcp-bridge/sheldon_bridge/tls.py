"""TLS for the bridge's WebSocket server.

If a cert + key are configured (`SHELDON_SSL_CERT`/`SHELDON_SSL_KEY`, or the
config.json `server.websocket_ssl_cert`/`_key`), the bridge serves `wss://`. If
those paths don't exist yet, a 10-year self-signed pair is generated in place —
so a fresh deploy gets encrypted transport with zero setup. For clients that
validate certs (e.g. console/crossplay), bring your own real cert by mounting
PEM files at those paths instead.
"""

from __future__ import annotations

import logging
import ssl
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_self_signed(cert: Path, key: Path) -> None:
    """Generate a 10-year self-signed RSA cert/key pair at the given paths."""
    cert.parent.mkdir(parents=True, exist_ok=True)
    key.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Generating 10-year self-signed TLS cert -> %s", cert)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
            "-days", "3650", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=sheldon-bridge",
        ],
        check=True,
        capture_output=True,
    )


def build_ssl_context(cert_path: str | None, key_path: str | None) -> ssl.SSLContext | None:
    """Return an SSLContext for `wss://` when a cert+key are configured (generating
    a self-signed pair if the files are missing), else None for plain `ws://`."""
    if not (cert_path and key_path):
        return None
    cert, key = Path(cert_path), Path(key_path)
    if not (cert.exists() and key.exists()):
        generate_self_signed(cert, key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return ctx
