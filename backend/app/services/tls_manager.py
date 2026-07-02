import asyncio
import datetime
import ipaddress
import logging
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

CERT_DIR = "/certs"
CERT_PATH = os.path.join(CERT_DIR, "cert.pem")
KEY_PATH = os.path.join(CERT_DIR, "key.pem")
ACME_WEBROOT = os.path.join(CERT_DIR, "acme-webroot")
LETSENCRYPT_DIR = os.path.join(CERT_DIR, "letsencrypt")

RENEWAL_CHECK_INTERVAL_SECONDS = 43200  # twice a day - certbot itself decides if renewal is actually due
EXPIRY_WARNING_DAYS = 14
EXPIRY_CHECK_INTERVAL_SECONDS = 86400  # once a day


def cert_exists() -> bool:
    return os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH)


def generate_self_signed(common_name: str = "lightnvr") -> None:
    """Uses the `cryptography` library directly rather than shelling out to
    openssl - it's already a dependency (via python-jose), so this needs no
    extra system package. Browsers will still show an untrusted-CA warning
    (expected and harmless to click through) since nothing signs this but
    itself; it exists purely to satisfy the HTTPS requirement for service
    workers and push notifications out of the box, with zero setup.
    """
    os.makedirs(CERT_DIR, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(common_name), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    _write_pair(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        cert.public_bytes(serialization.Encoding.PEM),
    )
    logger.info("Generated self-signed TLS certificate")


def _write_pair(key_bytes: bytes, cert_bytes: bytes) -> None:
    os.makedirs(CERT_DIR, exist_ok=True)
    tmp_key, tmp_cert = f"{KEY_PATH}.tmp", f"{CERT_PATH}.tmp"
    with open(tmp_key, "wb") as f:
        f.write(key_bytes)
    with open(tmp_cert, "wb") as f:
        f.write(cert_bytes)
    # Atomic swap so nginx's reload-watcher never catches a half-written file.
    os.replace(tmp_key, KEY_PATH)
    os.replace(tmp_cert, CERT_PATH)


def ensure_cert_exists() -> None:
    """Called at startup. nginx depends on the backend being healthy before
    it starts, so generating the default cert here guarantees it exists by
    the time nginx needs it - no separate init container required.
    """
    if not cert_exists():
        generate_self_signed()


def validate_cert_key_pair(cert_pem: bytes, key_pem: bytes) -> str | None:
    """Returns an error message if invalid, None if the pair is good."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        return f"Invalid certificate file: {exc}"

    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except ValueError as exc:
        return f"Invalid private key file: {exc}"

    cert_public_numbers = cert.public_key().public_numbers()
    key_public_numbers = key.public_key().public_numbers()
    if cert_public_numbers != key_public_numbers:
        return "Certificate and private key don't match"

    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc < now:
        return f"Certificate expired on {cert.not_valid_after_utc.date()}"

    return None


def save_custom_cert(cert_pem: bytes, key_pem: bytes) -> None:
    error = validate_cert_key_pair(cert_pem, key_pem)
    if error:
        raise ValueError(error)
    _write_pair(key_pem, cert_pem)
    logger.info("Installed custom TLS certificate")


async def _run_certbot(args: list[str]) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "certbot", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "certbot timed out"
    output = stdout.decode(errors="ignore")
    return proc.returncode == 0, output


async def request_letsencrypt_cert(domain: str, email: str) -> tuple[bool, str]:
    """HTTP-01 challenge via webroot - certbot writes challenge files to
    ACME_WEBROOT, which nginx must serve at /.well-known/acme-challenge/ on
    port 80. Requires the domain's DNS to point here and port 80 to be
    reachable from the internet (Let's Encrypt always checks port 80 for
    this challenge type, regardless of what port this app is normally
    reached on) - there's no way around that requirement, it's how the CA
    proves domain ownership.
    """
    os.makedirs(ACME_WEBROOT, exist_ok=True)
    os.makedirs(LETSENCRYPT_DIR, exist_ok=True)

    success, output = await _run_certbot(
        [
            "certonly",
            "--webroot",
            "-w", ACME_WEBROOT,
            "-d", domain,
            "--email", email,
            "--agree-tos",
            "--non-interactive",
            "--config-dir", LETSENCRYPT_DIR,
            "--work-dir", f"{LETSENCRYPT_DIR}/work",
            "--logs-dir", f"{LETSENCRYPT_DIR}/logs",
        ]
    )
    if not success:
        logger.warning("Let's Encrypt cert request failed: %s", output[-500:])
        return False, output[-500:]

    live_dir = f"{LETSENCRYPT_DIR}/live/{domain}"
    try:
        with open(f"{live_dir}/privkey.pem", "rb") as f:
            key_bytes = f.read()
        with open(f"{live_dir}/fullchain.pem", "rb") as f:
            cert_bytes = f.read()
    except OSError as exc:
        return False, f"Certbot reported success but cert files weren't found: {exc}"

    _write_pair(key_bytes, cert_bytes)
    logger.info("Obtained Let's Encrypt certificate for %s", domain)
    return True, "Certificate issued"


async def renew_letsencrypt_cert(domain: str) -> tuple[bool, str]:
    success, output = await _run_certbot(
        [
            "renew",
            "--webroot",
            "-w", ACME_WEBROOT,
            "--non-interactive",
            "--config-dir", LETSENCRYPT_DIR,
            "--work-dir", f"{LETSENCRYPT_DIR}/work",
            "--logs-dir", f"{LETSENCRYPT_DIR}/logs",
        ]
    )
    if not success:
        return False, output[-500:]

    live_dir = f"{LETSENCRYPT_DIR}/live/{domain}"
    if os.path.exists(f"{live_dir}/privkey.pem"):
        with open(f"{live_dir}/privkey.pem", "rb") as f:
            key_bytes = f.read()
        with open(f"{live_dir}/fullchain.pem", "rb") as f:
            cert_bytes = f.read()
        _write_pair(key_bytes, cert_bytes)
    return True, "ok"


def get_cert_expiry() -> datetime.datetime | None:
    if not cert_exists():
        return None
    with open(CERT_PATH, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return cert.not_valid_after_utc


async def cert_expiry_check_loop() -> None:
    """Covers every TLS mode, not just Let's Encrypt - a custom-uploaded cert
    or a renewal that's been silently failing (logged to last_renewal_error
    but never alerted) would otherwise only be noticed when a browser starts
    refusing the connection. Runs independently of letsencrypt_renewal_loop
    and checks the cert actually on disk, which is the ground truth regardless
    of why it might be stale.
    """
    from app.services.events import emit_event

    last_warned_date: datetime.date | None = None
    while True:
        try:
            expiry = get_cert_expiry()
            if expiry is not None:
                now = datetime.datetime.now(datetime.timezone.utc)
                days_left = (expiry - now).days
                if days_left <= EXPIRY_WARNING_DAYS and last_warned_date != now.date():
                    last_warned_date = now.date()
                    if days_left < 0:
                        message = (
                            f"The active HTTPS certificate expired on {expiry.date()} - HTTPS is broken until "
                            "it's renewed or replaced (Settings -> Security)."
                        )
                    else:
                        message = (
                            f"The active HTTPS certificate expires in {days_left} day(s) ({expiry.date()}) - "
                            "renew or replace it in Settings -> Security."
                        )
                    await emit_event(None, "system", message)
        except Exception:
            logger.exception("Cert expiry check failed")
        await asyncio.sleep(EXPIRY_CHECK_INTERVAL_SECONDS)


async def letsencrypt_renewal_loop() -> None:
    from app.db.session import AsyncSessionLocal
    from app.models.tls_settings import TlsSettings

    while True:
        try:
            async with AsyncSessionLocal() as db:
                settings_row = await db.get(TlsSettings, 1)
                if settings_row and settings_row.mode == "letsencrypt" and settings_row.domain:
                    success, message = await renew_letsencrypt_cert(settings_row.domain)
                    settings_row.last_renewal_at = datetime.datetime.now(datetime.timezone.utc)
                    settings_row.last_renewal_error = None if success else message
                    await db.commit()
        except Exception:
            logger.exception("Let's Encrypt renewal loop iteration failed")
        await asyncio.sleep(RENEWAL_CHECK_INTERVAL_SECONDS)
