"""Remote notification: email and push.

The SRS makes a remote notification mandatory on a confirmed alert, but the
network is not guaranteed: local sensing and local alerts must keep working
with no connection. So sending is always best-effort and never blocks the
sensor loop. An alert whose message could not be delivered stays flagged and
is retried when the connection returns.

Two channels are offered because they fail differently. Email needs an account
and an app password, which is a lot of setup to demonstrate. Push through ntfy
needs only a topic name, so it is the one most likely to be working when it
matters. Whichever are configured are all tried, and one success is enough.

Credentials come from the environment and are never logged.
"""

from __future__ import annotations

import logging
import smtplib
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage

from core.config import NtfySettings, SmtpSettings

logger = logging.getLogger(__name__)

#: Give up on a hung mail server rather than stalling the alert path.
SMTP_TIMEOUT_S = 10.0

#: The same, for a push server.
NTFY_TIMEOUT_S = 10.0

DISCLAIMER = "FreshGuard does not certify food safety. Inspect food before use."


@dataclass(frozen=True, slots=True)
class Notification:
    """One message to send."""

    subject: str
    body: str

    def as_email(self, sender: str, recipient: str) -> EmailMessage:
        """Render this notification as a plain-text email."""
        message = EmailMessage()
        message["Subject"] = self.subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content(f"{self.body}\n\n--\n{DISCLAIMER}\n")
        return message


def build_alert_notification(
    *,
    zone_name: str,
    alert_type: str,
    severity: str,
    message: str,
    timestamp: str,
    item_name: str | None = None,
) -> Notification:
    """Compose the message for a confirmed alert.

    A gas alert never names an item, so ``item_name`` must be left unset for
    one; passing it is a programming error rather than a formatting choice.
    """
    if alert_type == "gas_abnormal" and item_name is not None:
        raise ValueError("a gas alert describes a zone and must not name an item")

    subject = f"FreshGuard {severity}: {alert_type.replace('_', ' ')} in {zone_name}"
    lines = [
        message,
        "",
        f"Zone: {zone_name}",
        f"Type: {alert_type}",
        f"Severity: {severity}",
        f"Time (UTC): {timestamp}",
    ]
    if item_name is not None:
        lines.insert(3, f"Item: {item_name}")
    return Notification(subject=subject, body="\n".join(lines))


class Notifier(ABC):
    """Sends notifications somewhere."""

    @abstractmethod
    def send(self, notification: Notification) -> bool:
        """Attempt delivery. Returns True on success, False on failure.

        Never raises: a delivery problem must not interrupt the alert path.
        """

    @property
    def configured(self) -> bool:
        """True when this notifier has everything it needs to try."""
        return True


@dataclass
class FakeNotifier(Notifier):
    """Records notifications instead of sending them.

    ``fail`` makes every send report failure, which is how the offline retry
    path is tested.
    """

    sent: list[Notification] = field(default_factory=list)
    fail: bool = False

    def send(self, notification: Notification) -> bool:
        """Record the notification and report the configured outcome."""
        if self.fail:
            logger.warning("fake notifier refusing to send %r", notification.subject)
            return False
        self.sent.append(notification)
        return True


@dataclass
class SmtpNotifier(Notifier):
    """Sends over SMTP using credentials from the environment."""

    settings: SmtpSettings
    timeout_s: float = SMTP_TIMEOUT_S

    @property
    def configured(self) -> bool:
        """True when host, user, password and recipient are all present."""
        return self.settings.configured

    def send(self, notification: Notification) -> bool:
        """Attempt delivery, returning False rather than raising on failure."""
        if not self.configured:
            logger.warning("SMTP is not configured; %r not sent", notification.subject)
            return False

        email = notification.as_email(self.settings.sender, self.settings.recipient)
        try:
            with smtplib.SMTP(
                self.settings.host, self.settings.port, timeout=self.timeout_s
            ) as server:
                if self.settings.use_tls:
                    server.starttls()
                server.login(self.settings.user, self.settings.password)
                server.send_message(email)
        except (TimeoutError, smtplib.SMTPException, OSError) as exc:
            # The exception text can carry the server's reply but never our
            # credentials, so it is safe to log at warning level.
            logger.warning("could not send %r: %s", notification.subject, exc)
            return False

        logger.info("sent notification %r", notification.subject)
        return True


@dataclass
class NtfyNotifier(Notifier):
    """Sends a push notification through an ntfy server.

    ntfy takes the message as the body of an HTTP POST to the topic's URL, so
    there is no client library and nothing to install. A topic on a public
    server is readable by anyone who knows its name, which is why the settings
    ask for a long, unguessable one.
    """

    settings: NtfySettings
    timeout_s: float = NTFY_TIMEOUT_S

    @property
    def configured(self) -> bool:
        """True when a usable http(s) server and a topic are set.

        The scheme is checked here rather than trusted: the server comes from
        the environment, and a mistyped one naming ``file:`` would otherwise
        turn a notification into a local file read.
        """
        return self.settings.configured and self.settings.server.startswith(("http://", "https://"))

    def _request(self, notification: Notification) -> urllib.request.Request:
        """Build the POST. The title is a header; the body is the payload."""
        url = f"{self.settings.server.rstrip('/')}/{self.settings.topic}"
        headers = {
            "Title": notification.subject,
            # Read by the ntfy app to pick a sound and an icon.
            "Priority": "high",
            "Tags": "warning",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        body = f"{notification.body}\n\n{DISCLAIMER}".encode()
        return urllib.request.Request(  # noqa: S310 - scheme checked in `configured`
            url, data=body, headers=headers, method="POST"
        )

    def send(self, notification: Notification) -> bool:
        """Attempt delivery, returning False rather than raising on failure."""
        if not self.configured:
            logger.warning("ntfy is not configured; %r not sent", notification.subject)
            return False

        try:
            # `configured` has already established that the scheme is http(s).
            with urllib.request.urlopen(  # noqa: S310 - scheme checked in `configured`
                self._request(notification),
                timeout=self.timeout_s,
            ) as response:
                delivered = 200 <= response.status < 300  # noqa: PLR2004 - HTTP success range
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            logger.warning("could not push %r: %s", notification.subject, exc)
            return False

        if delivered:
            logger.info("pushed notification %r", notification.subject)
        return delivered


@dataclass
class CompositeNotifier(Notifier):
    """Tries every configured channel. One success is a delivered alert.

    Every channel is attempted even after one succeeds, because the point of
    having two is that the kitchen is told twice rather than once. A channel
    that is not configured is skipped without complaint.
    """

    notifiers: list[Notifier] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        """True when at least one channel could be attempted."""
        return any(notifier.configured for notifier in self.notifiers)

    def send(self, notification: Notification) -> bool:
        """Send through every configured channel. True if any succeeded."""
        if not self.configured:
            logger.warning(
                "no remote notification channel is configured; %r not sent",
                notification.subject,
            )
            return False

        results = [
            notifier.send(notification) for notifier in self.notifiers if notifier.configured
        ]
        delivered = any(results)
        if not delivered:
            logger.warning("every channel failed for %r", notification.subject)
        return delivered


def get_notifier(
    settings: SmtpSettings, ntfy: NtfySettings | None = None, *, fake: bool = False
) -> Notifier:
    """Return the notifier to use for these settings.

    Both channels are wired up when both are configured; the composite reports
    success when either one got through.
    """
    if fake:
        return FakeNotifier()
    if ntfy is None:
        return SmtpNotifier(settings)
    return CompositeNotifier([SmtpNotifier(settings), NtfyNotifier(ntfy)])
