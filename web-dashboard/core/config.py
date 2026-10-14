"""Typed application settings, read once from the environment.

Every tunable that is not a food threshold lives here. Thresholds and the
door-open timeout are admin-configurable and belong in the database, not in
this module.

Secrets are read from the environment (populated from an untracked ``.env``)
and are deliberately excluded from the dataclass reprs, so logging a settings
object can never leak the secret key, the SMTP password or the admin password.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

SENSOR_MODES = ("real", "sim")

#: Scenario names understood by the simulator backend. Kept here so that
#: configuration can be validated without importing the sensor package.
SIM_SCENARIOS = (
    "normal",
    "high_temp",
    "high_humidity",
    "gas_spike",
    "door_open",
    "sensor_disconnected",
    "dht_intermittent",
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


class ConfigError(ValueError):
    """Raised when the environment holds a value the application cannot use."""


def load_env_file(path: str | os.PathLike[str] | None = None) -> bool:
    """Load ``.env`` into ``os.environ`` if python-dotenv is installed.

    Existing environment variables always win, so a real deployment can
    override the file. The import is optional: ``monitor.py`` must still start
    on a Raspberry Pi where only the runtime dependencies are present.

    Returns True when a file was actually read.
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_FILE
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv is not installed; %s was not loaded", env_path)
        return False
    return bool(load_dotenv(env_path, override=False))


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(name, default).strip()


def _integer(env: Mapping[str, str], name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _text(env, name).lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean like 1/0 or true/false, got {raw!r}")


def _choice(env: Mapping[str, str], name: str, allowed: tuple[str, ...], default: str) -> str:
    value = _text(env, name).lower() or default
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Outgoing mail settings for the mandatory remote notification."""

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    recipient: str = ""
    use_tls: bool = True

    @property
    def configured(self) -> bool:
        """True when enough is set to attempt a send."""
        return bool(self.host and self.user and self.password and self.recipient)


@dataclass(frozen=True, slots=True)
class NtfySettings:
    """Push notification settings.

    ntfy delivers a push message to a phone running the ntfy app. It needs no
    account and no API key, which is why it is offered alongside email: the
    SRS makes a remote notification mandatory, and a notifier that needs no
    credentials is one that actually gets demonstrated.

    A topic on a public server is readable by anyone who knows its name, so a
    topic must be long and unguessable. The alert text names a storage zone and
    can name a food item; treat the topic as the only thing keeping that
    private.
    """

    server: str = "https://ntfy.sh"
    topic: str = ""
    #: Only needed for a topic protected by access control.
    token: str = field(default="", repr=False)

    @property
    def configured(self) -> bool:
        """True when a topic is set. Everything else has a usable default."""
        return bool(self.server and self.topic)


@dataclass(frozen=True, slots=True)
class FirebaseSettings:
    """Cloud replica settings. SQLite stays the source of truth regardless."""

    cred_path: Path | None = None
    db_url: str = ""
    sync_interval_s: int = 10

    @property
    def configured(self) -> bool:
        """True when a credential file and a database URL are both present."""
        return bool(self.db_url and self.cred_path is not None and self.cred_path.is_file())


@dataclass(frozen=True, slots=True)
class Settings:
    """All process configuration, resolved and validated."""

    secret_key: str = field(repr=False)
    db_path: Path
    host: str
    port: int
    debug: bool
    sensor_mode: str
    sim_scenario: str
    sim_control_file: Path | None
    poll_interval_s: float
    device_id: str
    admin_username: str
    admin_password: str = field(repr=False)
    smtp: SmtpSettings
    ntfy: NtfySettings
    firebase: FirebaseSettings
    tls_cert: Path | None
    tls_key: Path | None

    @property
    def simulated(self) -> bool:
        """True when the sensor simulator is in use rather than real hardware."""
        return self.sensor_mode == "sim"

    @property
    def tls_enabled(self) -> bool:
        """True when both halves of a TLS key pair are configured."""
        return self.tls_cert is not None and self.tls_key is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from a mapping, defaulting to the real environment."""
        env = os.environ if env is None else env

        secret_key = _text(env, "FG_SECRET_KEY")
        debug = _flag(env, "FG_DEBUG", default=False)
        if not secret_key:
            if not debug:
                raise ConfigError(
                    "FG_SECRET_KEY must be set. Copy .env.example to .env and fill it in."
                )
            # Development convenience only; sessions do not survive a restart.
            secret_key = "dev-insecure-key"  # noqa: S105 - placeholder, debug only
            logger.warning("FG_SECRET_KEY is unset; using an insecure development key")

        db_path = Path(_text(env, "FG_DB_PATH") or "freshguard.db")
        if not db_path.is_absolute():
            db_path = REPO_ROOT / db_path

        cred_raw = _text(env, "FIREBASE_CRED_PATH")
        control_raw = _text(env, "FG_SIM_CONTROL_FILE")
        cert_raw = _text(env, "FG_TLS_CERT")
        key_raw = _text(env, "FG_TLS_KEY")

        poll_interval_s = _integer(env, "FG_POLL_INTERVAL_S", 1, minimum=1)

        return cls(
            secret_key=secret_key,
            db_path=db_path,
            host=_text(env, "FG_HOST") or "0.0.0.0",  # noqa: S104 - LAN service by design
            port=_integer(env, "FG_PORT", 5000, minimum=1),
            debug=debug,
            sensor_mode=_choice(env, "FG_SENSOR_MODE", SENSOR_MODES, "sim"),
            sim_scenario=_choice(env, "FG_SIM_SCENARIO", SIM_SCENARIOS, "normal"),
            # Both processes share the database directory, so the control file
            # defaults to sitting beside it: the dashboard writes a scenario
            # there and monitor.py picks it up on its next tick without either
            # process needing to know about the other.
            sim_control_file=Path(control_raw) if control_raw else db_path.parent / "sim_scenario",
            poll_interval_s=float(poll_interval_s),
            device_id=_text(env, "FG_DEVICE_ID") or "pi-01",
            admin_username=_text(env, "FG_ADMIN_USERNAME") or "admin",
            admin_password=env.get("FG_ADMIN_PASSWORD", ""),
            smtp=SmtpSettings(
                host=_text(env, "SMTP_HOST"),
                port=_integer(env, "SMTP_PORT", 587, minimum=1),
                user=_text(env, "SMTP_USER"),
                password=env.get("SMTP_PASSWORD", ""),
                sender=_text(env, "SMTP_FROM") or _text(env, "SMTP_USER"),
                recipient=_text(env, "SMTP_TO"),
                use_tls=_flag(env, "SMTP_USE_TLS", default=True),
            ),
            ntfy=NtfySettings(
                server=_text(env, "NTFY_SERVER") or "https://ntfy.sh",
                topic=_text(env, "NTFY_TOPIC"),
                token=env.get("NTFY_TOKEN", ""),
            ),
            firebase=FirebaseSettings(
                cred_path=Path(cred_raw) if cred_raw else None,
                db_url=_text(env, "FIREBASE_DB_URL"),
                sync_interval_s=_integer(env, "FG_SYNC_INTERVAL_S", 10, minimum=1),
            ),
            tls_cert=Path(cert_raw) if cert_raw else None,
            tls_key=Path(key_raw) if key_raw else None,
        )


@lru_cache(maxsize=1)
def current_scenario(settings: Settings) -> str:
    """The simulated scenario in force right now.

    The control file wins when it holds a name the simulator knows, because
    that is what a running ``monitor.py`` is already obeying. Otherwise the
    configured default stands.
    """
    control = settings.sim_control_file
    if control is not None:
        try:
            requested = control.read_text(encoding="utf-8").strip()
        except OSError:
            # Nothing has chosen a scenario yet. Not worth a log line.
            return settings.sim_scenario
        if requested in SIM_SCENARIOS:
            return requested
        logger.warning("simulator control file holds unknown scenario %r", requested)
    return settings.sim_scenario


def get_settings() -> Settings:
    """Return the process-wide settings, reading ``.env`` on first use."""
    load_env_file()
    return Settings.from_env()
