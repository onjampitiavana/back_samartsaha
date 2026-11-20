"""
Outgoing e-mail (SMTP settings in core/config.py).

send() raises RecipientRefused when the receiving server rejects the
address itself (unknown mailbox), and MailError for anything else
(server unreachable, authentication, configuration).
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from core import config
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

_TIMEOUT_S = 20


class MailError(ServiceError):
    """The e-mail could not be sent."""


class RecipientRefused(MailError):
    """The mail server rejected the recipient address."""


def is_configured() -> bool:
    """True if e-mails can be sent (or logged, with MAIL_BACKEND=console)."""
    if config.MAIL_BACKEND == "console":
        return True
    return bool(config.SMTP_HOST and config.SMTP_FROM)


def send(to: str, subject: str, body: str) -> None:
    """Send a plain-text e-mail to `to`."""
    if config.MAIL_BACKEND == "console":
        log.warning("MAIL_BACKEND=console - e-mail NOT sent:\nTo: %s\nSubject: %s\n\n%s",
                    to, subject, body)
        return
    if not is_configured():
        raise MailError("L'envoi d'e-mails n'est pas configuré sur le serveur.")

    message = EmailMessage()
    message["From"] = config.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        if config.SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=_TIMEOUT_S,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=_TIMEOUT_S)
        with server:
            if config.SMTP_SECURITY == "starttls":
                server.starttls(context=ssl.create_default_context())
            if config.SMTP_USER and config.SMTP_PASSWORD:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(message)
    except smtplib.SMTPRecipientsRefused as exc:
        log.info("Recipient %s refused: %s", to, exc.recipients)
        raise RecipientRefused(f"Le serveur de messagerie a refusé l'adresse {to}.") from exc
    except smtplib.SMTPAuthenticationError as exc:
        log.error("SMTP login refused for %s on %s:%s: %s %s", config.SMTP_USER,
                  config.SMTP_HOST, config.SMTP_PORT, exc.smtp_code, exc.smtp_error)
        raise MailError("Le serveur de messagerie a refusé la connexion : identifiant ou mot "
                        "de passe incorrect.") from exc
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError) as exc:
        log.exception("Could not reach %s:%s", config.SMTP_HOST, config.SMTP_PORT)
        raise MailError("Le serveur de messagerie est momentanément injoignable.") from exc
    except smtplib.SMTPException as exc:
        log.exception("Could not send an e-mail to %s", to)
        raise MailError("Le serveur de messagerie a refusé l'envoi.") from exc
