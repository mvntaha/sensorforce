"""Generate a self-signed certificate so the camera scanner works on the LAN.

A browser will not hand a page the camera unless it was served over HTTPS or
came from localhost. The scanner is FreshGuard's front door, so without a
certificate it works only on the Pi itself and every phone on the network gets
the manual identifier box instead.

This writes a certificate for the Pi's own addresses. It is self-signed, so a
browser shows a warning the first time and someone has to accept it; that is
the price of not having a public domain name. Install the certificate on the
demo device beforehand if the warning would be awkward in front of an audience.

The key is written with owner-only permissions and both files are git-ignored.

Usage:
    python scripts/make_cert.py --host 192.168.1.42
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import logging
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

#: Long enough to outlive the project, short enough to be clearly temporary.
VALID_DAYS = 825


def local_addresses() -> list[str]:
    """The addresses this machine answers on, best effort."""
    found = {"127.0.0.1", "localhost"}
    try:
        hostname = socket.gethostname()
        found.add(hostname)
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        # A machine that cannot resolve its own name still gets localhost.
        logger.warning("could not determine this machine's addresses")
    return sorted(found)


def build(cert_path: Path, key_path: Path, hosts: list[str]) -> None:
    """Write a self-signed certificate and its private key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FreshGuard")])

    alternatives: list[x509.GeneralName] = []
    for host in hosts:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alternatives.append(x509.DNSName(host))

    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # The key is a secret even on a lab network.
    key_path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="An address the Pi answers on. Repeatable. Detected ones are added too.",
    )
    parser.add_argument("--cert", type=Path, default=REPO_ROOT / "secrets" / "freshguard.crt")
    parser.add_argument("--key", type=Path, default=REPO_ROOT / "secrets" / "freshguard.key")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    hosts = sorted({*args.host, *local_addresses()})
    build(args.cert, args.key, hosts)

    logger.info("certificate written for: %s", ", ".join(hosts))
    logger.info("  %s", args.cert)
    logger.info("  %s", args.key)
    logger.info("")
    logger.info("Add these to .env, then restart the app:")
    logger.info("  FG_TLS_CERT=%s", args.cert)
    logger.info("  FG_TLS_KEY=%s", args.key)
    logger.info("")
    logger.info("The certificate is self-signed, so the first visit from each")
    logger.info("device shows a warning that has to be accepted once.")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
